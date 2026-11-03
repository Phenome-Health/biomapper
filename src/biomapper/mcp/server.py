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
from typing import Annotated, Any, Literal, TypeVar

import anyio
import anyio.to_thread
from mcp.server.mcpserver import Context, ListRoots, MCPServer, Resolve
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ListRootsResult
from pydantic import BaseModel, Field

from biomapper._pins import collect_pins
from biomapper._version import resolve_version
from biomapper.client import BioMapperClient
from biomapper.exceptions import BioMapperError
from biomapper.mcp import tools
from biomapper.mcp.config import ServerConfig
from biomapper.mcp.gate import GateBusy, LoopGuard, LoopGuardTripped, RequestGate
from biomapper.mcp.jobs import JobManager, JobStatus, JobStatusList, roots_from_uris

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
    pins: dict[str, Any] | None = None
    pins_ready: anyio.Event = field(default_factory=anyio.Event)

    def provenance(self) -> tools.Provenance:
        return tools.build_provenance(
            pins=self.pins, base_url=self.config.base_url, kestrel_url=self.config.kestrel_url
        )

    async def load_pins(self) -> None:
        try:
            self.pins = await anyio.to_thread.run_sync(
                lambda: collect_pins(
                    probe=True,
                    base_url=self.config.base_url,
                    api_key=self.config.api_key,
                    kestrel_url=self.config.kestrel_url,
                ),
                abandon_on_cancel=True,
            )
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
            jobs = JobManager(cfg, gate=gate, client=jc)
            state = AppState(
                config=cfg,
                interactive=interactive,
                job_client=jc,
                gate=gate,
                guard=LoopGuard(cfg.loop_limit, cfg.loop_window_s, clock=cfg.monotonic),
                jobs=jobs,
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
        return tools.MapEntityResult(result=tools.shape_result(result),
                                     provenance=state.provenance())

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
        records = [{"name": e.name, "identifiers": dict(e.identifiers)} for e in entities]
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
            results=[tools.shape_result(r) for r in results], provenance=state.provenance()
        )

    _register_job_tools(server)
    return server


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
    async def cancel_job(job_id: str, ctx: Context[AppState, Any]) -> JobStatus:
        state = _state(ctx)
        try:
            return await state.jobs.cancel(job_id)
        except JobError as exc:
            raise ToolError(str(exc)) from None


def run() -> None:
    """Run the stdio server. Logs go to stderr; stdout carries only the MCP transport."""
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(name)s %(message)s"
    )
    build_server().run("stdio")
