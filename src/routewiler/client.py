"""Routewiler — the public async HTTP client."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from routewiler._auth import RoutewilerAuth
from routewiler.budgets.keystore import EnvelopeKeystore
from routewiler.budgets.local import DEFAULT_ENVELOPE_ID, BudgetStore, ensure_default_envelope
from routewiler.credentials.manifest_strategy import ManifestRecoveryStrategy
from routewiler.credentials.manifests.loader import ManifestRegistry
from routewiler.credentials.recovery import CredentialRecoverer, RecoveryStrategy
from routewiler.credentials.store import CredentialStore
from routewiler.errors import EnvelopeNotFoundError
from routewiler.funding import FundingSource
from routewiler.funding.evm import EvmFundingSource
from routewiler.policy.dsl import PolicyDocument, PolicyFile, compute_policy_hash, default_policy
from routewiler.policy.engine import PolicyEngine
from routewiler.rails import ADAPTER_REGISTRY, RailAdapter
from routewiler.routing.router import Router
from routewiler.routing.sticky import StickyCache
from routewiler.trace.emitter import TraceEmitter
from routewiler.trace.sink_sqlite import SqliteTraceSink


def _build_adapters(funding: list[FundingSource]) -> list[RailAdapter]:
    adapters: list[RailAdapter] = []
    for factory in ADAPTER_REGISTRY:
        adapter = factory(funding)
        if adapter is not None:
            adapters.append(adapter)
    return adapters


def _funding_label(funding: list[FundingSource]) -> str | None:
    if not funding:
        return None
    f = funding[0]
    if isinstance(f, EvmFundingSource):
        return f"evm:{f.network}:{f.asset}"
    return repr(f)


class Routewiler:
    """Async HTTP client that transparently handles 402 Payment Required.

    Mirrors the ``httpx.AsyncClient`` method surface (get/post/put/delete/
    patch/head/options/request).  Use as an async context manager to ensure
    the underlying connection pool is closed cleanly:

        async with Routewiler(funding=[Funding.base_usdc(wallet=signer)]) as c:
            resp = await c.get("https://api.vendor.com/data")

    Args:
        funding:         One or more funding sources (e.g. ``Funding.base_usdc(wallet=...)``).
        policy:          Optional policy file (``PolicyFile("policy.yaml")``). When omitted,
                         the built-in default policy is used (prefer x402, no rules).
        budget_envelope: ID of the envelope to draw from. Defaults to ``"default"``.
                         The envelope must exist in the database (use BudgetStore.create_envelope
                         to create custom envelopes before constructing the client).
                         Budget enforcement requires a trace_sink; if trace_sink is None,
                         no enforcement runs.
        trace_sink:      SQLite trace sink. Pass ``TraceSink.sqlite(path)`` to
                         enable local tracing. Defaults to ``None`` (no tracing).
        agent_id:        Optional identifier for the calling agent. Written into
                         TraceEvent.agent_id and used as part of the sticky routing key.
        session_id:      Optional session identifier. Used as part of the sticky routing
                         key so multiple logical sessions sharing one client get
                         independent sticky state.
    """

    def __init__(
        self,
        *,
        funding: list[FundingSource],
        policy: PolicyFile | PolicyDocument | None = None,
        budget_envelope: str | None = None,
        trace_sink: SqliteTraceSink | None = None,
        keystore_root: Path | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        recovery_strategy: RecoveryStrategy | None = None,
        manifest_paths: list[Path] | None = None,
    ) -> None:
        self._funding = funding
        self._trace_sink = trace_sink
        self._recovery_http: httpx.AsyncClient | None = None
        envelope_id = budget_envelope or DEFAULT_ENVELOPE_ID

        # Build the policy engine and compute the hash regardless of trace_sink.
        if isinstance(policy, PolicyFile):
            _policy_doc = policy.document
            _policy_hash = policy.policy_hash
        elif isinstance(policy, PolicyDocument):
            _policy_doc = policy
            _policy_hash = compute_policy_hash(policy)
        else:
            _policy_doc = default_policy()
            _policy_hash = compute_policy_hash(_policy_doc)
        policy_engine = PolicyEngine(_policy_doc)

        emitter: TraceEmitter | None = None
        budget_store: BudgetStore | None = None
        envelope_currency: str | None = None
        credential_store: CredentialStore | None = None
        recoverer: CredentialRecoverer | None = None

        if trace_sink is not None:
            keystore = (
                EnvelopeKeystore()
                if keystore_root is None
                else EnvelopeKeystore(root=keystore_root)
            )
            # Seed the default envelope row (idempotent INSERT OR IGNORE).
            ensure_default_envelope(trace_sink.db_path, keystore)

            budget_store = BudgetStore(trace_sink.db_path, keystore)

            # Resolve the envelope's declared currency from the DB.
            envelope_currency = budget_store.get_currency_sync(envelope_id)
            if envelope_currency is None:
                raise EnvelopeNotFoundError(
                    f"Envelope '{envelope_id}' not found. "
                    "Create it with BudgetStore.create_envelope() before constructing Routewiler."
                )

            emitter = TraceEmitter(
                sink=trace_sink,
                envelope_id=envelope_id,
                envelope_currency=envelope_currency,
                funding_label=_funding_label(funding),
                url_mode=trace_sink.url_mode,
                policy_hash=_policy_hash,
                agent_id=agent_id,
            )

            credential_store = CredentialStore(trace_sink.db_path)

            # Build the recovery strategy: user-supplied takes precedence.
            # Default: ManifestRecoveryStrategy with bundled manifests (or overridden paths).
            # A separate plain httpx.AsyncClient is used so recovery calls never trigger
            # Routewiler's own auth flow (we're replaying an existing credential, not paying).
            _effective_strategy: RecoveryStrategy
            if recovery_strategy is not None:
                _effective_strategy = recovery_strategy
            else:
                self._recovery_http = httpx.AsyncClient()
                registry = (
                    ManifestRegistry.from_paths(manifest_paths)
                    if manifest_paths
                    else ManifestRegistry.from_bundled()
                )
                _effective_strategy = ManifestRecoveryStrategy(
                    registry=registry,
                    client=self._recovery_http,
                )

            recoverer = CredentialRecoverer(
                store=credential_store,
                strategy=_effective_strategy,
                emitter=emitter,
            )

        self._budget_store = budget_store
        self._credential_store = credential_store
        self._emitter = emitter
        adapters = _build_adapters(funding)
        router = Router(adapters)
        sticky_cache = StickyCache()
        auth = RoutewilerAuth(
            router=router,
            sticky_cache=sticky_cache,
            funding=funding,
            agent_id=agent_id,
            session_id=session_id,
            emitter=emitter,
            budget_store=budget_store,
            envelope_id=envelope_id if budget_store is not None else None,
            envelope_currency=envelope_currency,
            policy_engine=policy_engine,
            credential_store=credential_store,
            recoverer=recoverer,
        )
        self._http = httpx.AsyncClient(auth=auth)

    # ------------------------------------------------------------------
    # Internal trace helper
    # ------------------------------------------------------------------

    async def _traced(self, coro: Any, ts_start: datetime) -> httpx.Response:
        """Execute an httpx coroutine and emit a passthrough trace if needed.

        The auth_flow marks paid responses with ``extensions["routewiler_emitted"]``.
        Any response that does not carry that flag gets a passthrough trace here.
        Errors raised by auth_flow (RailNotSupportedError, SigningError, etc.) have
        already been traced by auth_flow, so we let them propagate without re-tracing.

        If split-URL recovery succeeded, the auth_flow stashes the recovered 2xx on
        ``extensions["routewiler_recovered_response"]``.  We substitute it here so the
        caller receives the actual fulfilment response, not the original 4xx.
        """
        resp: httpx.Response = await coro
        # Substitute the recovered response if split-URL recovery succeeded.
        recovered: httpx.Response | None = resp.extensions.pop(
            "routewiler_recovered_response", None
        )
        if recovered is not None:
            resp = recovered
        ts_end = datetime.now(UTC)
        if self._emitter and not resp.extensions.get("routewiler_emitted"):
            await self._emitter.emit_passthrough(
                request=resp.request,
                response=resp,
                ts_start=ts_start,
                ts_end=ts_end,
            )
        return resp

    # ------------------------------------------------------------------
    # HTTP methods — delegate to the underlying AsyncClient
    # ------------------------------------------------------------------

    async def get(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self._traced(self._http.get(url, **kwargs), datetime.now(UTC))

    async def post(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self._traced(self._http.post(url, **kwargs), datetime.now(UTC))

    async def put(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self._traced(self._http.put(url, **kwargs), datetime.now(UTC))

    async def delete(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self._traced(self._http.delete(url, **kwargs), datetime.now(UTC))

    async def patch(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self._traced(self._http.patch(url, **kwargs), datetime.now(UTC))

    async def head(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self._traced(self._http.head(url, **kwargs), datetime.now(UTC))

    async def options(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self._traced(self._http.options(url, **kwargs), datetime.now(UTC))

    async def request(self, method: str, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self._traced(self._http.request(method, url, **kwargs), datetime.now(UTC))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        await self._http.aclose()
        if self._recovery_http is not None:
            await self._recovery_http.aclose()
        if self._budget_store is not None:
            await self._budget_store.aclose()
        if self._credential_store is not None:
            await self._credential_store.aclose()
        if self._trace_sink is not None:
            await self._trace_sink.aclose()

    async def start(self) -> None:
        """Start background tasks (reaper). Called automatically by __aenter__."""
        if self._budget_store is not None:
            await self._budget_store.start()

    async def __aenter__(self) -> Routewiler:
        await self._http.__aenter__()
        await self.start()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()
