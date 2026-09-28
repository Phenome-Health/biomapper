"""Async HTTP client for the BioMapper2 API."""

from __future__ import annotations

import asyncio
import contextlib
import os
import warnings
from collections import defaultdict
from collections.abc import AsyncGenerator, Iterable
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from biomapper.exceptions import (
    BioMapperAuthError,
    BioMapperConfigError,
    BioMapperRateLimitError,
    BioMapperServerError,
    BioMapperTimeoutError,
)
from biomapper.models import (
    AnnotatorInfo,
    BatchMappingResponse,
    EntityTypeInfo,
    MapEntityRequest,
    MappingResult,
    RawApiResult,
    VocabularyInfo,
)

DEFAULT_BASE_URL = "https://biomapper.expertintheloop.io/api/v1"
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_BATCH_SIZE = 1000  # /map/batch API limit (OpenAPI maxItems)


class BioMapperClient:
    """Async client for the BioMapper2 API.

    Handles authentication, request serialization, error mapping, and optional
    rate-limited batch processing.

    Usage (minimal, keyless against the hosted deployment)::

        async with BioMapperClient() as client:
            result = await client.map_entity("L-Histidine")
            print(result.primary_curie)   # "RM:0129894"

    Usage (with explicit key and hint)::

        async with BioMapperClient(api_key="sk-...") as client:
            result = await client.map_entity(
                name="4,6-DIOXOHEPTANOIC ACID",
                identifiers={"HMDB": "HMDB03349"},
            )

    Usage (force keyless even when ``BIOMAPPER_API_KEY`` is set)::

        async with BioMapperClient(anonymous=True) as client:
            result = await client.map_entity("L-Histidine")

    Args:
        api_key:    BioMapper API key. Optional. Defaults to the ``BIOMAPPER_API_KEY`` env var;
                    when neither is set the client is keyless.
        base_url:   API root URL.  Override for staging/local instances.
        timeout:    Per-request timeout in seconds.
        anonymous:  Send no ``X-API-Key`` header even if ``BIOMAPPER_API_KEY`` is set.

    Keyless by default (decided 2026-09-28, 1.5.4). The hosted deployment takes no key, so a
    client with no key configured sends no ``X-API-Key`` header rather than refusing to start.
    Earlier releases required ``anonymous=True`` to go keyless and raised on a missing key, so
    that a forgotten ``BIOMAPPER_API_KEY`` could not silently downgrade to an unauthenticated
    call. That protection is kept where it matters: against a deployment that does require a
    key, the first request still fails loudly, with a ``BioMapperAuthError`` saying a key is
    required, rather than at construction. A key from the argument or the environment is always
    sent, and no placeholder header is ever sent: an empty ``X-API-Key`` is a present-but-unknown
    key, which an authenticated deployment answers with 403 instead of the 401 that says a key
    is needed.

    Raises:
        BioMapperConfigError: If an explicit ``api_key`` is supplied together with
            ``anonymous=True``, an ambiguous instruction where guessing which was meant would
            either leak a key or silently drop one.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        anonymous: bool = False,
        **httpx_kwargs: Any,  # noqa: ANN401 — forwarded to httpx.AsyncClient verbatim
    ) -> None:
        resolved_key = api_key or os.getenv("BIOMAPPER_API_KEY")
        if anonymous and api_key:
            raise BioMapperConfigError(
                "anonymous=True was passed together with an explicit api_key. Refusing to guess "
                "which one you meant: drop the key to go keyless, or drop anonymous=True to "
                "authenticate."
            )
        # Keyless by default: no key configured means no header, not an error.
        anonymous = anonymous or not resolved_key
        self._anonymous = anonymous
        self._api_key = None if anonymous else resolved_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._httpx_kwargs = httpx_kwargs
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> BioMapperClient:
        # No header at all when keyless. An empty ``X-API-Key`` is a *present but unknown* key,
        # which an authenticated deployment answers with 403 rather than the 401 that would tell
        # the caller a key is needed.
        headers = {} if self._anonymous else {"X-API-Key": self._api_key or ""}
        self._client = httpx.AsyncClient(
            headers=headers,
            timeout=self._timeout,
            **self._httpx_kwargs,
        )
        return self

    async def __aexit__(self, *args: Any) -> None:  # noqa: ANN401 — dunder (exc_type, exc, tb) tuple
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError(
                "BioMapperClient must be used as an async context manager. "
                "Use `async with BioMapperClient() as client:`"
            )
        return self._client

    def _raise_for_status(self, response: httpx.Response) -> None:
        """Map HTTP status codes to typed exceptions."""
        code = response.status_code
        if code == 401 or code == 403:
            if self._anonymous:
                raise BioMapperAuthError(
                    f"This deployment requires an API key (HTTP {code}): pass api_key= or set "
                    "BIOMAPPER_API_KEY."
                )
            raise BioMapperAuthError(f"API key rejected (HTTP {code}).")
        if code == 429:
            retry_after: float | None = None
            if ra := response.headers.get("Retry-After"):
                with contextlib.suppress(ValueError):
                    retry_after = float(ra)
            raise BioMapperRateLimitError(
                "Rate limit exceeded (HTTP 429).", retry_after=retry_after
            )
        if code >= 500:
            raise BioMapperServerError(
                f"Server error (HTTP {code}): {response.text[:200]}",
                status_code=code,
            )
        response.raise_for_status()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def list_entity_types(self) -> list[EntityTypeInfo]:
        """Return the Biolink entity types supported by the API.

        Handles both response shapes:

        - **New (v2):** JSON array of ``{type, aliases?, defaultPrefixes?}``
          objects — each is validated directly into :class:`EntityTypeInfo`.
        - **Old (v1):** ``{entity_types: [...], aliases: {...}}`` dict — the
          alias map is inverted into per-type alias lists (backward compat).

        Returns:
            List of :class:`EntityTypeInfo`. For the new shape, order matches
            the server. For the old shape, order matches ``entity_types``.

        Raises:
            BioMapperAuthError: If the key is rejected.
            BioMapperServerError: For unrecoverable 5xx errors.
            BioMapperTimeoutError: If the request times out.
        """
        try:
            response = await self._http.get(f"{self._base_url}/entity-types")
        except httpx.TimeoutException as exc:
            raise BioMapperTimeoutError("list_entity_types timed out") from exc
        self._raise_for_status(response)
        payload = response.json()

        # New shape: array of EntityType objects
        if isinstance(payload, list):
            return [EntityTypeInfo.model_validate(item) for item in payload]

        # Old shape: {entity_types: [...], aliases: {...}} dict
        inverted: dict[str, list[str]] = defaultdict(list)
        for alias, type_name in payload.get("aliases", {}).items():
            inverted[type_name].append(alias)

        return [
            EntityTypeInfo(type=t, aliases=sorted(inverted.get(t, [])))
            for t in payload.get("entity_types", [])
        ]

    async def list_annotators(self) -> list[AnnotatorInfo]:
        """Return the annotators available to the mapping pipeline.

        Raises:
            BioMapperAuthError: If the key is rejected.
            BioMapperServerError: For unrecoverable 5xx errors.
            BioMapperTimeoutError: If the request times out.
        """
        try:
            response = await self._http.get(f"{self._base_url}/annotators")
        except httpx.TimeoutException as exc:
            raise BioMapperTimeoutError("list_annotators timed out") from exc
        self._raise_for_status(response)
        payload = response.json()
        return [AnnotatorInfo.model_validate(a) for a in payload.get("annotators", [])]

    async def list_vocabularies(self) -> list[VocabularyInfo]:
        """Return the identifier vocabularies supported by the API.

        Raises:
            BioMapperAuthError: If the key is rejected.
            BioMapperServerError: For unrecoverable 5xx errors.
            BioMapperTimeoutError: If the request times out.
        """
        try:
            response = await self._http.get(f"{self._base_url}/vocabularies")
        except httpx.TimeoutException as exc:
            raise BioMapperTimeoutError("list_vocabularies timed out") from exc
        self._raise_for_status(response)
        payload = response.json()
        return [VocabularyInfo.model_validate(v) for v in payload.get("vocabularies", [])]

    async def health_check(self) -> dict[str, Any]:
        """Verify connectivity and API readiness.

        Returns:
            The parsed health JSON, e.g.
            ``{"status": "healthy", "version": "0.1.0", "mapper_initialized": True}``.

        Raises:
            BioMapperAuthError: If the key is rejected.
            BioMapperServerError: If the service is not healthy.
        """
        try:
            response = await self._http.get(f"{self._base_url}/health")
        except httpx.TimeoutException as exc:
            raise BioMapperTimeoutError("Health check timed out") from exc
        self._raise_for_status(response)
        return dict(response.json())

    async def map_entity(
        self,
        name: str,
        entity_type: str = "biolink:SmallMolecule",
        identifiers: dict[str, str | list[str]] | None = None,
        annotation_mode: str = "missing",
        annotators: list[str] | None = None,
        *,
        vocab: str | list[str] | None = None,
        array_delimiters: list[str] | None = None,
        prefer_human: bool | None = None,
        prefer_canonical: bool | None = None,
        candidate_limit: int | None = None,
        kestrel_top_n: int | None = None,
    ) -> MappingResult:
        """Map a single entity name to standardized knowledge-graph identifiers.

        Args:
            name:            Compound or entity name to resolve.
            entity_type:     Biolink entity type.  Use ``"biolink:SmallMolecule"``
                             for metabolites.
            identifiers:     Optional pre-existing IDs used as resolver hints,
                             e.g. ``{"HMDB": "HMDB00177"}``.
            annotation_mode: ``"missing"`` (default), ``"all"``, or ``"none"``.
            annotators:      Optional list of annotator names to use. When not
                             specified, BioMapper2 uses all available annotators.
                             Use ``["kestrel-vector-search"]`` for strict matching.
            vocab:           Allowed vocabulary name(s) to map to, e.g. ``"refmet"``.
            array_delimiters: Characters used to split delimited ID strings.
            prefer_human:    For gene/protein entities, prefer the human (HGNC-bearing)
                             candidate over a wrong-species ortholog. Server default ``True``.
            prefer_canonical: For non-gene categories with a canonical-namespace policy,
                             prefer the canonical-namespace node. Server default ``True``.
            candidate_limit: Candidates each Kestrel search annotator retrieves (1..100).
            kestrel_top_n:   Opt in to raw Kestrel passthrough rows on
                             :attr:`MappingResult.kestrel_results` (1..100). Passthrough only:
                             it never changes ``chosen_kg_id``, ``assigned_ids`` or the
                             certificate.

        Any option left as ``None`` is omitted from the request, so the server's own default
        applies and the payload is unchanged for callers who do not use these.

        Returns:
            A :class:`~biomapper.models.MappingResult` with resolved identifiers.

        Raises:
            ValueError: If ``candidate_limit`` or ``kestrel_top_n`` is outside 1..100.
            BioMapperAuthError: If the API key is rejected.
            BioMapperRateLimitError: If the API signals throttling.
            BioMapperServerError: For unrecoverable 5xx errors.
            BioMapperTimeoutError: If the request times out.
        """
        options = self._build_options(
            annotation_mode,
            annotators,
            vocab=vocab,
            array_delimiters=array_delimiters,
            prefer_human=prefer_human,
            prefer_canonical=prefer_canonical,
            candidate_limit=candidate_limit,
            kestrel_top_n=kestrel_top_n,
        )

        payload = MapEntityRequest(
            name=name,
            entity_type=entity_type,
            identifiers=identifiers or {},
            options=options,
        )

        hmdb_hint = self._hmdb_hint(identifiers or {})

        try:
            response = await self._http.post(
                f"{self._base_url}/map/entity",
                json=payload.model_dump(exclude_none=False),
            )
        except httpx.TimeoutException as exc:
            raise BioMapperTimeoutError(f"Request timed out for '{name}'") from exc

        self._raise_for_status(response)
        data = dict[str, Any](response.json())
        return MappingResult.from_api_response(data, query_name=name, hmdb_hint=hmdb_hint)

    async def map_entities(
        self,
        records: Iterable[dict[str, Any]],
        entity_type: str = "biolink:SmallMolecule",
        annotation_mode: str = "missing",
        annotators: list[str] | None = None,
        progress: bool = False,
        *,
        vocab: str | list[str] | None = None,
        array_delimiters: list[str] | None = None,
        prefer_human: bool | None = None,
        prefer_canonical: bool | None = None,
        candidate_limit: int | None = None,
        kestrel_top_n: int | None = None,
    ) -> list[MappingResult]:
        """Map a batch of entity records via the native ``/map/batch`` endpoint.

        Each record is a dict with at least a ``"name"`` key, and optionally
        ``"identifiers"`` (``{"HMDB": "HMDB00177"}``). Inputs are auto-chunked
        at ``DEFAULT_MAX_BATCH_SIZE`` entities per request (API limit).

        Args:
            records:           Iterable of ``{"name": str, "identifiers": dict}`` dicts.
                               Materialized into a list at entry so generators are
                               accepted.
            entity_type:       Biolink entity type applied to every record.
            annotation_mode:   Annotation mode applied to every record.
            annotators:        Optional list of annotator names to use.
            progress:          Show a tqdm progress bar (requires ``biomapper[notebook]``).
                               The bar totals ``len(records)`` and advances by the
                               chunk size after each chunk completes.

        Returns:
            List of :class:`~biomapper.models.MappingResult`, one per input record,
            in input order. Records that fail (either per-record errors in a
            successful response or every record in a chunk-level HTTP failure)
            return a result with ``error`` set rather than raising.

        Raises:
            asyncio.CancelledError: Propagated immediately so callers can cancel
                mid-batch. All other exceptions are caught and surfaced as
                per-record errors.

        Example::

            async with BioMapperClient() as client:
                results = await client.map_entities(
                    [
                        {"name": "L-Histidine"},
                        {"name": "Glucose", "identifiers": {"HMDB": "HMDB00122"}},
                    ],
                    progress=True,
                )
        """
        records = list(records)  # materialize so generators work and len() is safe

        # Built once: the option block is identical for every record, and building it here means
        # an out-of-range bound raises before any request is sent rather than per chunk.
        options = self._build_options(
            annotation_mode,
            annotators,
            vocab=vocab,
            array_delimiters=array_delimiters,
            prefer_human=prefer_human,
            prefer_canonical=prefer_canonical,
            candidate_limit=candidate_limit,
            kestrel_top_n=kestrel_top_n,
        )

        requests = [
            MapEntityRequest(
                name=str(r.get("name", "")),
                entity_type=entity_type,
                identifiers=dict(r.get("identifiers") or {}),
                options=dict(options),
            )
            for r in records
        ]

        chunks = [
            requests[i : i + DEFAULT_MAX_BATCH_SIZE]
            for i in range(0, len(requests), DEFAULT_MAX_BATCH_SIZE)
        ]

        pbar: Any = None
        if progress:
            try:
                from tqdm.auto import tqdm

                pbar = tqdm(total=len(records), desc="Mapping entities")
            except ImportError:
                pass  # silently degrade if tqdm not installed

        results: list[MappingResult] = []

        try:
            for chunk in chunks:

                try:
                    response = await self._http.post(
                        f"{self._base_url}/map/batch",
                        json={
                            "entities": [
                                r.model_dump(exclude_none=False) for r in chunk
                            ]
                        },
                    )
                    self._raise_for_status(response)
                    parsed = BatchMappingResponse.model_validate(response.json())
                    # strict=True: a length mismatch between sent entities and
                    # returned results raises ValueError, which the chunk-level
                    # except below converts into per-record errors — preserving the
                    # invariant that len(results) == len(records) end-to-end.
                    for req, raw in zip(chunk, parsed.results, strict=True):
                        if raw.name and raw.name != req.name:
                            warnings.warn(
                                f"Batch order mismatch: sent {req.name!r}, "
                                f"got {raw.name!r}",
                                RuntimeWarning,
                                stacklevel=2,
                            )
                        results.append(
                            MappingResult.from_batch_entry(
                                raw,
                                query_name=req.name,
                                hmdb_hint=self._hmdb_hint(req.identifiers),
                            )
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — broad catch preserves "one bad chunk doesn't abort the batch"
                    for req in chunk:
                        results.append(
                            MappingResult(
                                query_name=req.name,
                                hmdb_hint=self._hmdb_hint(req.identifiers),
                                error=str(exc),
                            )
                        )
                    if isinstance(exc, BioMapperRateLimitError) and exc.retry_after:
                        # Courtesy: honor server's Retry-After between chunks.
                        await asyncio.sleep(exc.retry_after)
                finally:
                    if pbar is not None:
                        pbar.update(len(chunk))
        finally:
            # Outer finally ensures pbar.close() runs even if CancelledError
            # bubbles out of the loop (Jupyter widget cleanup).
            if pbar is not None:
                pbar.close()

        return results

    @staticmethod
    def _hmdb_hint(identifiers: dict[str, str | list[str]]) -> str | None:
        """Echo back the HMDB hint. The API accepts a list per vocabulary, so unwrap one."""
        value = identifiers.get("HMDB")
        if isinstance(value, list):
            return str(value[0]) if value else None
        return value

    @staticmethod
    def _check_bounds(name: str, value: int | None) -> None:
        """Reject an out-of-range 1..100 option locally.

        The API bounds ``candidate_limit`` and ``kestrel_top_n`` to 1..100 and answers 422.
        Failing here turns a wasted round trip into an immediate, self-describing error.
        """
        if value is not None and not (1 <= value <= 100):
            raise ValueError(f"{name} must be between 1 and 100, got {value}")

    @staticmethod
    def _build_options(
        annotation_mode: str,
        annotators: list[str] | None,
        *,
        vocab: str | list[str] | None = None,
        array_delimiters: list[str] | None = None,
        prefer_human: bool | None = None,
        prefer_canonical: bool | None = None,
        candidate_limit: int | None = None,
        kestrel_top_n: int | None = None,
    ) -> dict[str, Any]:
        """Assemble the ``options`` block, omitting anything the caller left unset.

        An unset option is left out entirely rather than sent as ``None``, so the server's own
        default governs and the wire payload stays byte-identical to the pre-existing one for
        callers who pass nothing new.
        """
        BioMapperClient._check_bounds("candidate_limit", candidate_limit)
        BioMapperClient._check_bounds("kestrel_top_n", kestrel_top_n)

        options: dict[str, Any] = {"annotation_mode": annotation_mode}
        for key, value in (
            ("annotators", annotators),
            ("vocab", vocab),
            ("array_delimiters", array_delimiters),
            ("prefer_human", prefer_human),
            ("prefer_canonical", prefer_canonical),
            ("candidate_limit", candidate_limit),
            ("kestrel_top_n", kestrel_top_n),
        ):
            if value is not None:
                options[key] = value
        return options

    async def map_dataset_file_iter(
        self,
        path: Path,
        *,
        name_column: str,
        provided_id_columns: list[str],
        entity_type: str = "biolink:SmallMolecule",
        annotation_mode: str = "missing",
        annotators: list[str] | None = None,
        vocab: str | None = None,
        prefer_human: bool | None = None,
        prefer_canonical: bool | None = None,
        candidate_limit: int | None = None,
        kestrel_top_n: int | None = None,
    ) -> AsyncGenerator[MappingResult, None]:
        """Stream per-row mapping results from ``POST /map/dataset/stream``.

        Uploads ``path`` as a multipart body and yields :class:`MappingResult`
        per NDJSON line as the server emits them. The iterator is the
        streaming primitive used by :func:`biomapper.map_dataset_file_sync`
        and is also available to any async caller (e.g. the Entity Linker UI).

        Args:
            path:                Path to a TSV or CSV file.
            name_column:         Column name containing entity names (required).
            provided_id_columns: Columns carrying pre-existing identifiers, e.g.
                                 ``["hmdb_id"]`` (required). Values must not
                                 contain commas — see Raises below.
            entity_type:         Biolink entity type applied to every row.
            annotation_mode:     ``"missing"`` | ``"all"`` | ``"none"``.
            annotators:          Optional list of annotator names. ``None``
                                 uses all available annotators. Values must
                                 not contain commas.
            vocab:               Optional vocabulary hint forwarded to the API.

        Yields:
            :class:`~biomapper.models.MappingResult` per NDJSON line. The
            ``hmdb_hint`` attribute is always ``None`` on yielded results — the
            server processes the dataset opaquely and the client has no
            per-row hint to echo back. The ``confidence_score`` attribute is
            also ``None``: the dataset-stream endpoint emits a slimmer
            per-row payload than ``/map/batch`` and omits the ``assigned_ids``
            block where per-annotator scores live. Use :func:`map_entity` /
            :func:`map_entities` if you need confidence scores.

        Raises:
            ValueError: If a value in ``provided_id_columns`` or ``annotators``
                contains a comma — these would silently split at the wire and
                corrupt the request.
            BioMapperAuthError: On initial-request 401/403.
            BioMapperRateLimitError: On initial-request 429.
            BioMapperServerError: On initial-request 5xx.
            BioMapperTimeoutError: On connect timeout (initial request only;
                mid-stream timeouts propagate as the raw ``httpx`` exception).
            httpx.HTTPStatusError: On initial-request 4xx other than 401/403/429
                (notably 422 validation errors from the server).
            httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.NetworkError:
                On mid-stream transport failures. Values yielded before the
                failure are delivered; the exception propagates when the
                caller awaits the next ``__anext__``.

        Stream truncation:
            If the connection closes without a trailing newline on the final
            line, ``aiter_lines()`` raises ``httpx.RemoteProtocolError`` after
            yielding all complete lines (treated as transport-level — propagates).
            If the server emits a syntactically-incomplete line followed by
            a newline, the line reaches the parser and is yielded as a
            per-row error :class:`MappingResult` (treated as per-record).

        Concurrency:
            Runs one upload + stream through the shared ``httpx.AsyncClient``.
            Running two ``map_dataset_file_iter`` calls concurrently on the
            same :class:`BioMapperClient` (e.g. via ``asyncio.gather``) may
            serialize or deadlock depending on the connection-pool
            configuration. For concurrent dataset jobs, construct a separate
            :class:`BioMapperClient` per job.

        Recommended timeout for long runs:
            The default ``timeout=30.0`` applies per-phase; the read phase
            times out if the server pauses more than 30 s between NDJSON
            lines. For datasets whose server-side annotator lookups exceed
            30 s, construct the client with
            ``BioMapperClient(timeout=httpx.Timeout(read=None, connect=30.0))``.
        """
        params = self._dataset_query_params(
            entity_type=entity_type,
            name_column=name_column,
            provided_id_columns=provided_id_columns,
            annotation_mode=annotation_mode,
            annotators=annotators,
            vocab=vocab,
            prefer_human=prefer_human,
            prefer_canonical=prefer_canonical,
            candidate_limit=candidate_limit,
            kestrel_top_n=kestrel_top_n,
        )
        content_type = self._dataset_content_type(path)

        # AsyncExitStack enforces LIFO cleanup: the HTTP stream context exits
        # first (any httpx finalization that reads from fh completes), and
        # only then does the file close. If cancellation fires during TLS
        # handshake or initial response-headers read, both resources unwind
        # correctly — no chance of closing fh while httpx still holds it.
        async with contextlib.AsyncExitStack() as stack:
            fh = stack.enter_context(path.open("rb"))
            try:
                response = await stack.enter_async_context(
                    self._http.stream(
                        "POST",
                        f"{self._base_url}/map/dataset/stream",
                        files={"file": (path.name, fh, content_type)},
                        params=params,
                    )
                )
            except httpx.TimeoutException as exc:
                raise BioMapperTimeoutError(
                    f"Dataset stream request timed out for {path.name!r}"
                ) from exc

            # `_raise_for_status` reads `response.text` on 5xx; that errors on a
            # still-streaming response. Eagerly consume the body for non-2xx
            # so the shared error mapper works uniformly for streaming and
            # non-streaming callers.
            if response.status_code >= 400:
                await response.aread()
            self._raise_for_status(response)

            async for line in response.aiter_lines():
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    raw = RawApiResult.model_validate_json(stripped)
                except ValidationError as exc:
                    yield MappingResult(
                        query_name="<unknown>",
                        error=f"Failed to parse NDJSON line: {exc}",
                    )
                    continue
                yield MappingResult.from_batch_entry(
                    raw, query_name=raw.name or "", hmdb_hint=None
                )

    @staticmethod
    def _dataset_query_params(
        *,
        entity_type: str,
        name_column: str,
        provided_id_columns: list[str],
        annotation_mode: str,
        annotators: list[str] | None,
        vocab: str | None,
        prefer_human: bool | None = None,
        prefer_canonical: bool | None = None,
        candidate_limit: int | None = None,
        kestrel_top_n: int | None = None,
    ) -> dict[str, str]:
        """Serialize dataset endpoint query params.

        ``provided_id_columns`` and ``annotators`` are joined with commas for
        the wire form. Commas inside any value are rejected as a ``ValueError``
        at the boundary — silently splitting ``"iupac,name"`` into two
        columns would corrupt the request undetectably.

        The dataset routes take the mapping options as query params rather than an options
        object, and ``array_delimiters`` is not among them, so it has no dataset equivalent.
        An option left as ``None`` is omitted so the server default applies.
        """
        BioMapperClient._reject_commas("provided_id_columns", provided_id_columns)
        if annotators is not None:
            BioMapperClient._reject_commas("annotators", annotators)
        BioMapperClient._check_bounds("candidate_limit", candidate_limit)
        BioMapperClient._check_bounds("kestrel_top_n", kestrel_top_n)

        params: dict[str, str] = {
            "entity_type": entity_type,
            "name_column": name_column,
            "provided_id_columns": ",".join(provided_id_columns),
            "annotation_mode": annotation_mode,
        }
        if annotators is not None:
            params["annotators"] = ",".join(annotators)
        if vocab is not None:
            params["vocab"] = vocab
        # Booleans go on the wire lowercased, which is what FastAPI's bool parser expects.
        if prefer_human is not None:
            params["prefer_human"] = str(prefer_human).lower()
        if prefer_canonical is not None:
            params["prefer_canonical"] = str(prefer_canonical).lower()
        if candidate_limit is not None:
            params["candidate_limit"] = str(candidate_limit)
        if kestrel_top_n is not None:
            params["kestrel_top_n"] = str(kestrel_top_n)
        return params

    @staticmethod
    def _reject_commas(param_name: str, values: list[str]) -> None:
        for v in values:
            if "," in v:
                raise ValueError(
                    f"{param_name!r} values must not contain commas: {v!r}"
                )

    @staticmethod
    def _dataset_content_type(path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix == ".tsv":
            return "text/tab-separated-values"
        if suffix == ".csv":
            return "text/csv"
        return "application/octet-stream"
