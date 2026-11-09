"""The BioMapper MCP server: lifespan, shared state, and tool registration.

One process holds two :class:`~biomapper.client.BioMapperClient` instances (a 35 s interactive
client and a 300 s job client, since a client fixes its timeout at construction) behind ONE
:class:`~biomapper.mcp.gate.RequestGate`. The one-in-flight invariant comes from the gate, not
from sharing a client.

The lifespan never blocks MCP ``initialize`` on the network: pin collection (``/health`` on the
API and on Kestrel) runs as a background task, and tools report ``pins_status: pending`` until it
lands.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar

import anyio
import httpx
from mcp.server.mcpserver import Context, ListRoots, MCPServer, Resolve
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ListRootsResult
from pydantic import BaseModel, ConfigDict, Field

from biomapper._pins import KESTREL_LABEL, kg_build_label
from biomapper._provenance import fetch_kg_build_info
from biomapper._version import resolve_version
from biomapper.client import BioMapperClient
from biomapper.exceptions import BioMapperError
from biomapper.mcp import candidates as cands
from biomapper.mcp import judgments as judg
from biomapper.mcp import tools
from biomapper.mcp._storage import utc
from biomapper.mcp.config import ServerConfig
from biomapper.mcp.gate import (
    CandidateBudget,
    CandidateBudgetExhausted,
    GateBusy,
    LoopGuard,
    LoopGuardTripped,
    RequestGate,
)
from biomapper.mcp.jobs import (
    JobError,
    JobManager,
    JobStatus,
    JobStatusList,
    probe_pins,
    roots_from_uris,
)
from biomapper.mcp.references import (
    CandidateSet,
    RefEngine,
    RefError,
    RefRecord,
    RefSource,
    RefStore,
)
from biomapper.models import MappingResult

logger = logging.getLogger("biomapper.mcp")

T = TypeVar("T")

AnnotationMode = Literal["missing", "all", "none"]


@dataclass
class AppState:
    """Everything the tools share for the life of one server process (one MCP session)."""

    config: ServerConfig
    interactive: BioMapperClient
    job_client: BioMapperClient
    gate: RequestGate
    guard: LoopGuard
    jobs: JobManager
    refs: RefStore
    candidate_budget: CandidateBudget
    pins: dict[str, Any] | None = None
    pins_ready: anyio.Event = field(default_factory=anyio.Event)

    def provenance(self) -> tools.Provenance:
        return tools.build_provenance(
            pins=self.pins, base_url=self.config.base_url, kestrel_url=self.config.kestrel_url
        )

    async def load_pins(self) -> None:
        try:
            # The API probe waits for the shared gate like any request, so it never overlaps a
            # mapping call. It runs as a background task, so initialize never waits on it.
            self.pins = await probe_pins(self.config, self.gate)
        finally:
            self.pins_ready.set()


class BatchEntity(BaseModel):
    name: str
    identifiers: dict[str, str] = Field(default_factory=dict)


class ServerInfo(BaseModel):
    server_version: str
    provenance: tools.Provenance
    batch_cap: int
    loop_limit: int
    loop_window_s: float
    allowed_roots: list[str]


def _state(ctx: Context[Any, Any]) -> AppState:
    state = ctx.request_context.lifespan_context
    assert isinstance(state, AppState)
    return state


def _shape_and_flag(
    state: AppState,
    result: MappingResult,
    *,
    source: RefSource,
    entity_type: str,
    identifiers: dict[str, str],
    annotation_mode: str,
) -> tools.EntityResult:
    """Shape one result; a weak row gets ``weak: true`` and, if it can be stored, a ``ref``.

    Failing to store a ref never fails or alters the mapping itself.
    """
    shaped = tools.shape_result(result)
    trigger = tools.weak_trigger(result, entity_type)
    if trigger is None:
        return shaped
    shaped.weak = True
    try:
        record = state.refs.issue(
            source=source,
            query={"name": result.query_name, "entity_type": entity_type,
                   "identifiers": identifiers, "annotation_mode": annotation_mode},
            result=result,
            weak_trigger=trigger,
            kg_build=state.provenance().kg_build,
        )
    except OSError:
        logger.warning("could not store a ref for a weak row", exc_info=True)
        return shaped
    shaped.ref = record.ref_id
    return shaped


def _want_roots(ctx: Context[Any, Any]) -> ListRoots | ListRootsResult:
    """Ask the client for its roots only when it advertises them; otherwise none."""
    caps = ctx.client_capabilities
    if caps is None or caps.roots is None:
        return ListRootsResult(roots=[])
    return ListRoots()


ClientRoots = Annotated[ListRootsResult, Resolve(_want_roots)]


async def _gated(
    state: AppState, call: Callable[[], Awaitable[T]], *, count: bool
) -> T:
    """Run one interactive request: loop guard, gate (with budget), and a wall-clock deadline.

    The deadline covers the gate wait too, because httpx timeouts are per phase and a slow
    response could otherwise outlive Claude Desktop's ~60 s cancel.
    """
    token: float | None = None
    if count:
        try:
            token = state.guard.record()
        except LoopGuardTripped as exc:
            raise ToolError(str(exc)) from None
    try:
        with anyio.fail_after(state.config.tool_deadline_s):
            async with state.gate.hold(wait_budget_s=state.config.gate_wait_s):
                return await call()
    except GateBusy as exc:
        if token is not None:
            state.guard.release(token)
        raise ToolError(str(exc)) from None
    except TimeoutError:
        raise ToolError(
            f"BioMapper did not answer within {state.config.tool_deadline_s:g} s; prod is slow "
            "right now. For more than one or two entities, write them to a TSV/CSV and use "
            "map_file, which runs as a background job and waits as long as prod needs."
        ) from None
    except BioMapperError as exc:
        raise ToolError(tools.explain_client_error(exc)) from None


Outcome = tuple[T | None, str | None]


async def _gated_partial(
    state: AppState,
    calls: list[Callable[[], Awaitable[T]]],
    *,
    on_attempt: Callable[[], None],
) -> list[Outcome[T]]:
    """Run several requests under ONE wall-clock deadline, one gate hold each, never all-or-nothing.

    Unlike :func:`_gated`, a failure is a per-call reason, not a ToolError: ``deadline`` for every
    call the deadline cut off (including one in flight), ``busy`` when the gate stayed held (by a
    job chunk) past ``gate_wait_s``, and otherwise the HTTP or transport class of the failure.
    ``on_attempt`` runs as each call starts, which is when it is charged.
    """
    outcomes: list[Outcome[T]] = [(None, "deadline")] * len(calls)
    with anyio.move_on_after(state.config.tool_deadline_s):
        for index, call in enumerate(calls):
            on_attempt()
            try:
                async with state.gate.hold(wait_budget_s=state.config.gate_wait_s):
                    outcomes[index] = (await call(), None)
            except GateBusy:
                outcomes[index] = (None, "busy")
            except (BioMapperError, httpx.HTTPError, OSError) as exc:
                outcomes[index] = (None, cands.reason_for_exception(exc))
    return outcomes


def _load_ref(state: AppState, ref: str, roots: list[Path]) -> RefRecord:
    """Load a ref the session may use: interactive by home+uid, a job ref by the job's folders."""
    try:
        record = state.refs.load(ref)
    except RefError as exc:
        raise ToolError(str(exc)) from None
    if record.source == "job":
        job = state.jobs._load(record.job_id or "")
        if job is None:
            raise ToolError(f"Reference {ref!r} belongs to a job that no longer exists.")
        if not state.jobs._within(job, state.jobs.allowed_roots(roots)):
            raise ToolError(
                f"Reference {ref!r} belongs to a job whose files are outside the folders allowed "
                "in this session."
            )
    return record


async def _await_pins(state: AppState, limit_s: float = 5.0) -> None:
    with anyio.move_on_after(limit_s):
        await state.pins_ready.wait()


def build_server(config: ServerConfig | None = None) -> MCPServer[AppState]:
    """Build the server. ``config=None`` reads the environment at lifespan start."""

    @asynccontextmanager
    async def lifespan(app: MCPServer[AppState]) -> AsyncIterator[AppState]:
        cfg = config or ServerConfig.from_env()
        key_kwargs: dict[str, Any] = (
            {"api_key": cfg.api_key} if cfg.api_key else {"anonymous": True}
        )
        gate = RequestGate()
        async with (
            BioMapperClient(
                base_url=cfg.base_url, timeout=cfg.interactive_timeout_s, **key_kwargs
            ) as interactive,
            BioMapperClient(base_url=cfg.base_url, timeout=cfg.job_timeout_s, **key_kwargs) as jc,
            anyio.create_task_group() as tg,
        ):
            refs = RefStore(cfg.home, wall_clock=cfg.wall_clock, ttl_s=cfg.ref_ttl_s)
            jobs = JobManager(cfg, gate=gate, client=jc, refs=refs)
            state = AppState(
                config=cfg,
                interactive=interactive,
                job_client=jc,
                gate=gate,
                guard=LoopGuard(cfg.loop_limit, cfg.loop_window_s, clock=cfg.monotonic),
                jobs=jobs,
                refs=refs,
                candidate_budget=CandidateBudget(
                    cfg.candidate_budget, cfg.candidate_window_s, clock=cfg.monotonic
                ),
            )
            tg.start_soon(state.load_pins)
            tg.start_soon(jobs.run_worker)
            tg.start_soon(jobs.auto_resume, roots_from_uris([]))
            try:
                yield state
            finally:
                jobs.close()
                tg.cancel_scope.cancel()

    server: MCPServer[AppState] = MCPServer(
        "biomapper",
        title="BioMapper entity resolution",
        instructions=tools.SERVER_INSTRUCTIONS,
        version=resolve_version(),
        website_url="https://github.com/Phenome-Health/biomapper",
        lifespan=lifespan,
    )

    # ------------------------------------------------------------------
    # Discovery (gated, not counted by the loop guard)
    # ------------------------------------------------------------------

    @server.tool(description=tools.DISCOVERY_DESCRIPTIONS["list_entity_types"])
    async def list_entity_types(ctx: Context[AppState, Any]) -> tools.EntityTypesResult:
        state = _state(ctx)
        items = await _gated(state, state.interactive.list_entity_types, count=False)
        return tools.EntityTypesResult(entity_types=[
            tools.EntityTypeEntry(type=i.type, aliases=i.aliases,
                                  default_prefixes=i.default_prefixes)
            for i in items
        ])

    @server.tool(description=tools.DISCOVERY_DESCRIPTIONS["list_vocabularies"])
    async def list_vocabularies(ctx: Context[AppState, Any]) -> tools.VocabulariesResult:
        state = _state(ctx)
        items = await _gated(state, state.interactive.list_vocabularies, count=False)
        return tools.VocabulariesResult(vocabularies=[
            tools.VocabularyEntry(prefix=v.prefix, aliases=v.aliases) for v in items
        ])

    @server.tool(description=tools.DISCOVERY_DESCRIPTIONS["list_annotators"])
    async def list_annotators(ctx: Context[AppState, Any]) -> tools.AnnotatorsResult:
        state = _state(ctx)
        items = await _gated(state, state.interactive.list_annotators, count=False)
        return tools.AnnotatorsResult(annotators=[
            tools.AnnotatorEntry(slug=a.slug, name=a.name, description=a.description)
            for a in items
        ])

    @server.tool(
        description="Server version, KG provenance (pending until the startup probe lands), "
        "limits, and the folders map_file may read. Makes no request to BioMapper."
    )
    async def server_info(ctx: Context[AppState, Any], roots: ClientRoots) -> ServerInfo:
        state = _state(ctx)
        allowed = state.jobs.allowed_roots(roots_from_uris(roots.roots))
        return ServerInfo(
            server_version=resolve_version(),
            provenance=state.provenance(),
            batch_cap=state.config.batch_cap,
            loop_limit=state.config.loop_limit,
            loop_window_s=state.config.loop_window_s,
            allowed_roots=[str(r) for r in allowed],
        )

    # ------------------------------------------------------------------
    # Interactive mapping (gated and counted)
    # ------------------------------------------------------------------

    @server.tool(description=tools.MAP_ENTITY_DESCRIPTION)
    async def map_entity(
        name: str,
        entity_type: str,
        ctx: Context[AppState, Any],
        identifiers: dict[str, str] | None = None,
        annotation_mode: AnnotationMode = "missing",
    ) -> tools.MapEntityResult:
        state = _state(ctx)
        result = await _gated(
            state,
            lambda: state.interactive.map_entity(
                name,
                entity_type=entity_type,
                identifiers=dict(identifiers or {}),
                annotation_mode=annotation_mode,
            ),
            count=True,
        )
        shaped = _shape_and_flag(
            state, result, source="entity", entity_type=entity_type,
            identifiers=dict(identifiers or {}), annotation_mode=annotation_mode,
        )
        return tools.MapEntityResult(result=shaped, provenance=state.provenance())

    @server.tool(description=tools.MAP_BATCH_DESCRIPTION)
    async def map_batch(
        entities: list[BatchEntity],
        entity_type: str,
        ctx: Context[AppState, Any],
        annotation_mode: AnnotationMode = "missing",
    ) -> tools.MapBatchResult:
        state = _state(ctx)
        cap = state.config.batch_cap
        if len(entities) > cap:
            raise ToolError(
                f"map_batch takes at most {cap} entities (got {len(entities)}); prod answers "
                f"about 4-5 s per entity and a tool call must finish within ~60 s. Write the "
                f"entities to a TSV/CSV file and use map_file, which runs a resumable background "
                f"job."
            )
        if not entities:
            raise ToolError("map_batch needs at least one entity.")
        records: list[dict[str, Any]] = [
            {"name": e.name, "identifiers": dict(e.identifiers)} for e in entities
        ]
        results = await _gated(
            state,
            lambda: state.interactive.map_entities(
                records,
                entity_type=entity_type,
                annotation_mode=annotation_mode,
                sleep_on_rate_limit=False,
            ),
            count=True,
        )
        failure = tools.systemic_error(results, len(records))
        if failure is not None:
            raise ToolError(
                f"The whole batch failed: {tools.rate_limit_hint(failure)} No per-entity results "
                f"were returned."
            )
        return tools.MapBatchResult(
            results=[
                _shape_and_flag(
                    state, r, source="batch", entity_type=entity_type,
                    identifiers=dict(rec["identifiers"]), annotation_mode=annotation_mode,
                )
                for r, rec in zip(results, records, strict=True)
            ],
            provenance=state.provenance(),
        )

    # ------------------------------------------------------------------
    # Candidate review (1.7.0): weak rows only, own budget, partial results
    # ------------------------------------------------------------------

    @server.tool(description=cands.GET_CANDIDATES_DESCRIPTION)
    async def get_candidates(
        refs: list[str],
        ctx: Context[AppState, Any],
        roots: ClientRoots,
        n: int | None = None,
    ) -> cands.GetCandidatesResult:
        state = _state(ctx)
        cfg = state.config
        top_n = cfg.candidates_default_n if n is None else n
        if not 1 <= top_n <= cfg.candidates_max_n:
            raise ToolError(f"n must be between 1 and {cfg.candidates_max_n} (got {top_n}).")
        wanted = list(dict.fromkeys(refs))
        if not wanted:
            raise ToolError("get_candidates needs at least one ref.")
        if len(wanted) > cfg.candidates_refs_per_call:
            raise ToolError(
                f"get_candidates takes at most {cfg.candidates_refs_per_call} refs per call (got "
                f"{len(wanted)}); each is a full re-map on the shared deployment. Split them "
                f"across calls."
            )
        client_roots = roots_from_uris(roots.roots)
        records = [_load_ref(state, ref, client_roots) for ref in wanted]
        strong = [r.ref_id for r in records if r.weak_trigger is None]
        if strong:
            raise ToolError(
                f"Reference(s) {', '.join(strong)} are not weak (the engine's answer was "
                "corroborated on the re-map that issued them), so there is nothing to review. "
                "Use the candidates already returned with them, or record_suggestion."
            )
        if state.candidate_budget.available() < len(records):
            raise ToolError(state.candidate_budget.refusal(len(records)))

        def remap(record: RefRecord) -> Callable[[], Awaitable[MappingResult]]:
            q = record.query
            if record.source == "entity":
                return lambda: state.interactive.map_entity(
                    q.name, entity_type=q.entity_type, identifiers=dict(q.identifiers),
                    annotation_mode=q.annotation_mode, kestrel_top_n=top_n,
                )

            async def one() -> MappingResult:
                results = await state.interactive.map_entities(
                    [{"name": q.name, "identifiers": dict(q.identifiers)}],
                    entity_type=q.entity_type, annotation_mode=q.annotation_mode,
                    kestrel_top_n=top_n, sleep_on_rate_limit=False,
                )
                if len(results) != 1:
                    raise BioMapperError("batch of one returned a different number of results")
                return results[0]

            return one

        def charge() -> None:
            try:
                state.candidate_budget.charge()
            except CandidateBudgetExhausted as exc:  # pre-checked; only a race lands here
                raise ToolError(str(exc)) from None

        await _await_pins(state)
        outcomes = await _gated_partial(state, [remap(r) for r in records], on_attempt=charge)
        build = await _fresh_build(state)
        return cands.GetCandidatesResult(
            results=[
                _finish_ref(state, record, result, reason, top_n, build)
                for record, (result, reason) in zip(records, outcomes, strict=True)
            ],
            provenance=state.provenance(),
        )

    @server.tool(description=judg.RECORD_SUGGESTION_DESCRIPTION)
    async def record_suggestion(
        ref: str,
        choice: str,
        rationale: str,
        ctx: Context[AppState, Any],
        roots: ClientRoots,
    ) -> judg.SuggestionResult:
        state = _state(ctx)
        record = _load_ref(state, ref, roots_from_uris(roots.roots))
        try:
            canonical, source, backing = judg.validate_choice(record, choice)
        except judg.JudgmentError as exc:
            raise ToolError(str(exc)) from None
        job = None
        if record.source == "job":
            job = state.jobs._load(record.job_id or "")
            if job is None:
                raise ToolError(f"Reference {ref!r} belongs to a job that no longer exists.")
            try:
                state.jobs.check_choices_writable(job)
            except JobError as exc:
                raise ToolError(str(exc)) from None
        store = judg.SuggestionStore(state.refs, wall_clock=state.config.wall_clock)
        suggestion = store.record(record, canonical, source, rationale, backing)
        choices_path: str | None = None
        if job is not None:
            try:
                choices_path = str(state.jobs.write_choices(job, store.job_rows(job.job_id)))
            except (JobError, OSError) as exc:
                raise ToolError(
                    f"The suggestion was recorded ({suggestion.suggestion_id}), but "
                    f"{job.choices_path.name} could not be rewritten: {exc}"
                ) from None
        return judg.SuggestionResult(
            suggestion_id=suggestion.suggestion_id,
            ref=record.ref_id,
            choice=suggestion.choice,
            choice_source=suggestion.choice_source,
            supersedes=suggestion.supersedes,
            recorded_utc=suggestion.recorded_utc,
            choices_path=choices_path,
        )

    # Status-like keys (status, human_confirmed, ...) must be refused, not silently dropped: a
    # suggestion has no status, and an agent that thinks it set one would misreport it.
    _forbid_extra_arguments(server, "record_suggestion")

    _register_job_tools(server)
    return server


def _forbid_extra_arguments(server: MCPServer[AppState], name: str) -> None:
    """Make one tool reject unknown arguments (``additionalProperties: false``).

    The SDK's argument model ignores extra keys by default and exposes no per-tool switch, so this
    swaps in a strict subclass. ``test_status_like_extra_keys_are_rejected_by_the_schema`` fails
    loudly if an SDK upgrade changes these internals.
    """
    tool = server._tool_manager.get_tool(name)
    assert tool is not None
    base = tool.fn_metadata.arg_model
    strict: type[BaseModel] = type(base.__name__, (base,), {
        "model_config": ConfigDict(**base.model_config, extra="forbid"),
        "__module__": base.__module__,
    })
    tool.fn_metadata.arg_model = strict  # type: ignore[assignment]
    tool.parameters = strict.model_json_schema(by_alias=True)


async def _fresh_build(state: AppState, limit_s: float = 5.0) -> str:
    """The KG build Kestrel reports now, read right after a re-map.

    The startup probe can be hours old in a long-lived session; verifying a re-map against it
    would miss a build change. A fresh read also refreshes the session's provenance. When
    Kestrel does not answer (in ``limit_s``), the last known build is used.
    """
    url = state.config.kestrel_url
    fresh: tuple[str, Any, str | None] | None = None
    with anyio.move_on_after(limit_s):
        fresh = await anyio.to_thread.run_sync(
            lambda: fetch_kg_build_info(url), abandon_on_cancel=True
        )
    if fresh is None or fresh[2] is not None:
        logger.warning("could not re-read the KG build; using the last known one")
        return state.provenance().kg_build
    version, kg, _ = fresh
    kestrel = {"url": url, "kestrel_version": version, "kg_build": kg.model_dump(),
               "error": None, "label": KESTREL_LABEL}
    if state.pins is not None:
        state.pins = {**state.pins, "kestrel": {**(state.pins.get("kestrel") or {}), **kestrel}}
    return kg_build_label({"kestrel": kestrel})


def _store_set(state: AppState, record: RefRecord, cset: CandidateSet) -> None:
    """Store a fetched set on the ref, merged into its latest copy on disk under its lock."""

    def put(fresh: RefRecord) -> None:
        if fresh.candidates is not None:
            fresh.candidate_history.append(fresh.candidates)
        fresh.candidates = cset

    try:
        state.refs.update(record.ref_id, put)
    except OSError:
        logger.warning("could not store candidates on %s", record.ref_id, exc_info=True)


def _finish_ref(
    state: AppState,
    record: RefRecord,
    result: MappingResult | None,
    reason: str | None,
    n: int,
    build: str,
) -> cands.RefCandidates:
    """Turn one re-map outcome into the ref's status, and store what was fetched on the ref."""
    now = utc(state.config.wall_clock())
    out = cands.RefCandidates(
        ref=record.ref_id,
        query_name=record.query.name,
        weak_trigger=record.weak_trigger,
        engine_choice=record.engine_choice,
        certificate_state=record.engine.certificate_state,
        status="fetch_failed",
        reason=reason,
        n=n,
    )
    row_error = None if result is None else cands.remap_error(result)
    if result is None or row_error is not None:
        out.reason = reason if result is None else row_error
        _store_set(state, record, CandidateSet(status="fetch_failed", reason=out.reason, n=n,
                                               fetched_utc=now))
        return out
    mismatch = cands.verify(record, result, build)
    if mismatch is not None:
        # Anchor a fresh ref to the re-map so the agent can carry on deliberately. The original
        # keeps its engine answer; its stored set is marked unusable.
        status, why, items, raw = cands.compact(result, RefEngine.of(result), n=n)
        out.status, out.reason = "run_mismatch", mismatch
        _store_set(state, record, CandidateSet(status="run_mismatch", reason=mismatch, n=n,
                                               fetched_utc=now, raw_sha256=raw))
        try:
            new = state.refs.issue(
                source=record.source,
                job_id=record.job_id,
                row_key=record.row_key,
                query=record.query.model_dump(),
                result=result,
                weak_trigger=tools.weak_trigger(result, record.query.entity_type),
                kg_build=build,
                supersedes=record.ref_id,
            )
        except (OSError, RefError):
            logger.warning("could not issue a superseding ref", exc_info=True)
            return out
        _store_set(state, new, CandidateSet(status=status, reason=why, n=n, fetched_utc=now,
                                            raw_sha256=raw, items=items))
        record.superseded_by = new.ref_id
        try:
            state.refs.update(record.ref_id, lambda fresh: setattr(fresh, "superseded_by",
                                                                  new.ref_id))
        except OSError:
            logger.warning("could not link %s to its successor", record.ref_id, exc_info=True)
        out.superseding_ref = new.ref_id
        out.candidates = items
        return out
    status, why, items, raw = cands.compact(result, record.engine, n=n)
    out.status, out.reason, out.candidates = status, why, items
    _store_set(state, record, CandidateSet(status=status, reason=why, n=n, fetched_utc=now,
                                           raw_sha256=raw, items=items))
    return out


def _register_job_tools(server: MCPServer[AppState]) -> None:
    from biomapper.mcp.jobs import JOB_TOOL_DESCRIPTIONS, JobError

    @server.tool(description=JOB_TOOL_DESCRIPTIONS["map_file"])
    async def map_file(
        path: str,
        name_column: str,
        entity_type: str,
        ctx: Context[AppState, Any],
        roots: ClientRoots,
        identifier_columns: dict[str, str] | None = None,
        output_path: str | None = None,
        annotation_mode: AnnotationMode = "missing",
        key_column: str | None = None,
        rerun: bool = False,
    ) -> JobStatus:
        state = _state(ctx)
        try:
            return await state.jobs.submit(
                path=path,
                name_column=name_column,
                entity_type=entity_type,
                identifier_columns=dict(identifier_columns or {}),
                output_path=output_path,
                annotation_mode=annotation_mode,
                key_column=key_column,
                rerun=rerun,
                client_roots=roots_from_uris(roots.roots),
            )
        except JobError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(description=JOB_TOOL_DESCRIPTIONS["job_status"])
    async def job_status(
        ctx: Context[AppState, Any], roots: ClientRoots, job_id: str | None = None
    ) -> JobStatusList:
        state = _state(ctx)
        try:
            return await state.jobs.status(job_id, client_roots=roots_from_uris(roots.roots))
        except JobError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(description=JOB_TOOL_DESCRIPTIONS["resume_job"])
    async def resume_job(
        job_id: str, ctx: Context[AppState, Any], roots: ClientRoots
    ) -> JobStatus:
        state = _state(ctx)
        try:
            return await state.jobs.resume(job_id, client_roots=roots_from_uris(roots.roots))
        except JobError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(description=JOB_TOOL_DESCRIPTIONS["cancel_job"])
    async def cancel_job(
        job_id: str, ctx: Context[AppState, Any], roots: ClientRoots
    ) -> JobStatus:
        state = _state(ctx)
        try:
            return await state.jobs.cancel(job_id, client_roots=roots_from_uris(roots.roots))
        except JobError as exc:
            raise ToolError(str(exc)) from None


def run() -> None:
    """Run the stdio server. Logs go to stderr; stdout carries only the MCP transport."""
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(name)s %(message)s"
    )
    build_server().run("stdio")
