"""Routeweiler — the public async HTTP client."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from routeweiler._auth import RouteweilerAuth
from routeweiler.budgets.ecb_provider import EcbRateProvider
from routeweiler.budgets.fmv_provider import FmvProvider
from routeweiler.budgets.keystore import EnvelopeKeystore
from routeweiler.budgets.local import BudgetStore
from routeweiler.budgets.schema import BudgetEnvelope, EnvelopeCurrency
from routeweiler.credentials.manifest_strategy import ManifestRecoveryStrategy
from routeweiler.credentials.manifests.loader import ManifestRegistry
from routeweiler.credentials.recovery import CredentialRecoverer, RecoveryStrategy
from routeweiler.credentials.store import CredentialStore
from routeweiler.errors import EnvelopeNotFoundError
from routeweiler.funding import FundingSource
from routeweiler.funding.evm import EvmFundingSource
from routeweiler.funding.lightning import LightningFundingSource
from routeweiler.funding.stripe import StripeFundingSource
from routeweiler.funding.tempo import TempoFundingSource
from routeweiler.normalized import Rail
from routeweiler.policy.dsl import Policy
from routeweiler.policy.engine import PolicyEngine
from routeweiler.rails import ADAPTER_REGISTRY, RailAdapter
from routeweiler.routing.router import Router
from routeweiler.routing.sticky import StickyCache
from routeweiler.trace.emitter import TraceEmitter
from routeweiler.trace.sink_sqlite import SqliteTraceSink


class _EnvelopesNamespace:
    """User-facing namespace for envelope management.

    Obtain via ``client.envelopes``; requires a ``trace_sink`` on the parent
    ``Routeweiler`` instance:

        async with Routeweiler(funding=[...], trace_sink=TraceSink.sqlite("trace.db")) as c:
            await c.envelopes.create(
                "my-envelope",
                cap_minor_units=100_00,    # 100.00 USD in cents
                cap_currency="usd",
                allowed_rails=["x402", "l402"],
                ttl_seconds=86_400,
            )
    """

    def __init__(self, store: BudgetStore | None) -> None:
        self._store = store

    def _require_store(self) -> BudgetStore:
        if self._store is None:
            raise RuntimeError(
                "Envelope management requires a trace_sink. "
                "Pass trace_sink=TraceSink.sqlite(...) when constructing Routeweiler."
            )
        return self._store

    async def create(
        self,
        envelope_id: str,
        *,
        cap_minor_units: int,
        cap_currency: EnvelopeCurrency,
        allowed_rails: list[Rail],
        allowed_origins_glob: list[str] | None = None,
        ttl_seconds: int,
        owner_agent_id: str | None = None,
    ) -> None:
        """Create a new spending envelope.

        Args:
            envelope_id:          Unique identifier for this envelope.
            cap_minor_units:      Spending cap in the currency's minor units
                                  (e.g. cents for USD, pence for GBP).
            cap_currency:         ISO 4217 currency code (e.g. ``"usd"``).
            allowed_rails:        Rails permitted for draws (e.g. ``["x402", "l402"]``).
            allowed_origins_glob: URL glob patterns allowed to draw from this envelope.
                                  Defaults to ``["*"]`` (any origin).
            ttl_seconds:          Envelope lifetime in seconds from now.
            owner_agent_id:       Optional agent identifier for this envelope.

        Raises:
            sqlite3.IntegrityError: Envelope with this ID already exists.
            RuntimeError:          ``trace_sink`` was not configured on the client.
        """
        store = self._require_store()
        await store.create_envelope(
            envelope_id,
            cap_minor_units=cap_minor_units,
            cap_currency=cap_currency,
            allowed_rails=allowed_rails,
            allowed_origins_glob=allowed_origins_glob,
            ttl_seconds=ttl_seconds,
            owner_agent_id=owner_agent_id,
        )


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
    if isinstance(f, LightningFundingSource):
        return f"lightning:{f.network}"
    if isinstance(f, TempoFundingSource):
        return f"mpp-tempo:{f.network}:{f.asset}"
    if isinstance(f, StripeFundingSource):
        return f"stripe:{f.currency}:{f.payment_method}"
    return repr(f)


class Routeweiler:
    """Async HTTP client that transparently handles 402 Payment Required.

    Mirrors the ``httpx.AsyncClient`` method surface (get/post/put/delete/
    patch/head/options/request).  Use as an async context manager to ensure
    the underlying connection pool is closed cleanly:

        async with Routeweiler(funding=[Funding.base_usdc(wallet=signer)]) as c:
            resp = await c.get("https://api.vendor.com/data")

    Args:
        funding:         One or more funding sources (e.g. ``Funding.base_usdc(wallet=...)``).
        policy:          Optional ``Policy`` instance. When omitted, the built-in
                         default is used (prefer x402, no rules).
        budget_envelope: Controls which spending envelope the client draws from.
                         Three forms are accepted:

                         * ``None`` (default) — no budget enforcement.  Payments are
                           made without any cap; trace events are still written when
                           ``trace_sink`` is set, but ``envelope_id`` will be ``None``.
                         * ``str`` — ID of a pre-existing envelope.  The envelope must
                           already be present in the database; ``EnvelopeNotFoundError``
                           is raised at construction time if it is missing.
                         * ``BudgetEnvelope`` — declarative spec.  The envelope is
                           created idempotently inside ``__aenter__`` (i.e. the first
                           ``async with Routeweiler(...) as client:`` call).  If an
                           envelope with the same ``id`` already exists it is reused
                           unchanged.

                         Budget enforcement requires a ``trace_sink``; if ``trace_sink``
                         is ``None`` no enforcement runs regardless of this argument.
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
        policy: Policy | None = None,
        budget_envelope: str | BudgetEnvelope | None = None,
        trace_sink: SqliteTraceSink | None = None,
        keystore_root: Path | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        recovery_strategy: RecoveryStrategy | None = None,
        fmv_provider: FmvProvider | None = None,
        ecb_provider: EcbRateProvider | None = None,
    ) -> None:
        self._funding = funding
        self._trace_sink = trace_sink
        self._recovery_http: httpx.AsyncClient | None = None
        self._pending_envelope_spec: BudgetEnvelope | None = None

        envelope_id: str | None
        if isinstance(budget_envelope, BudgetEnvelope):
            envelope_id = budget_envelope.id
            self._pending_envelope_spec = budget_envelope
        elif isinstance(budget_envelope, str):
            envelope_id = budget_envelope
        else:
            envelope_id = None

        # Build the policy engine and compute the hash regardless of trace_sink.
        _policy = policy if policy is not None else Policy()
        _policy_hash = _policy.policy_hash
        policy_engine = PolicyEngine(_policy)

        emitter: TraceEmitter | None = None
        budget_store: BudgetStore | None = None
        envelope_currency: EnvelopeCurrency | None = None
        envelope_allowed_rails: list[Rail] = []
        credential_store: CredentialStore | None = None
        recoverer: CredentialRecoverer | None = None

        if trace_sink is not None:
            keystore = (
                EnvelopeKeystore()
                if keystore_root is None
                else EnvelopeKeystore(root=keystore_root)
            )
            budget_store = BudgetStore(
                trace_sink.db_path,
                keystore,
                fmv_provider=fmv_provider,
                ecb_provider=ecb_provider,
            )

            # Resolve the envelope's declared currency and allowed rails from the DB.
            # Skip when no envelope was supplied (no enforcement) or when a
            # declarative BudgetEnvelope was supplied (the envelope may not exist yet;
            # it is created idempotently in start() instead).
            if envelope_id is not None and self._pending_envelope_spec is None:
                envelope_currency = budget_store.get_envelope_currency_sync(envelope_id)
                if envelope_currency is None:
                    raise EnvelopeNotFoundError(
                        f"Envelope '{envelope_id}' not found. "
                        "Create it with BudgetStore.create_envelope() before constructing "
                        "Routeweiler, or pass a BudgetEnvelope as budget_envelope."
                    )
                envelope_allowed_rails = budget_store.get_envelope_allowed_rails_sync(envelope_id)
            else:
                # No envelope or spec path: currency/allowed_rails are None/empty.
                # For specs, they are populated in start() after creation.
                envelope_currency = None
                envelope_allowed_rails = []

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
            # A separate plain httpx.AsyncClient is used so recovery calls never trigger
            # Routeweiler's own auth flow (we're replaying an existing credential, not paying).
            _effective_strategy: RecoveryStrategy
            if recovery_strategy is not None:
                _effective_strategy = recovery_strategy
            else:
                self._recovery_http = httpx.AsyncClient()
                _effective_strategy = ManifestRecoveryStrategy(
                    registry=ManifestRegistry.from_bundled(),
                    client=self._recovery_http,
                )

            recoverer = CredentialRecoverer(
                store=credential_store,
                strategy=_effective_strategy,
                emitter=emitter,
            )

        # Resolve the reference currency for FMV / max_per_call enforcement.
        # Precedence: envelope cap_currency > policy.currency > None.
        # For deferred BudgetEnvelope specs the envelope currency comes from the spec
        # directly (the row doesn't exist yet); bind_envelope() will confirm it in start().
        _spec_currency: EnvelopeCurrency | None = (
            self._pending_envelope_spec.cap_currency
            if self._pending_envelope_spec is not None
            else None
        )
        reference_currency: EnvelopeCurrency | None = (
            envelope_currency or _spec_currency or _policy.currency
        )

        # Guard: if any rule declares max_per_call_minor_units we need a reference
        # currency to evaluate the limit.  Fail at construction rather than silently
        # paying uncapped amounts at runtime.
        if reference_currency is None and any(
            r.max_per_call_minor_units is not None for r in _policy.rules
        ):
            raise ValueError(
                "Policy contains max_per_call_minor_units rules but no reference currency "
                "is available. Set Policy(currency='usd') or configure a budget_envelope "
                "so Routeweiler knows what unit max_per_call_minor_units is measured in."
            )

        self._budget_store = budget_store
        self._envelopes = _EnvelopesNamespace(budget_store)
        self._credential_store = credential_store
        self._emitter = emitter
        adapters = _build_adapters(funding)
        router = Router(adapters)
        sticky_cache = StickyCache()
        auth = RouteweilerAuth(
            router=router,
            sticky_cache=sticky_cache,
            funding=funding,
            agent_id=agent_id,
            session_id=session_id,
            emitter=emitter,
            budget_store=budget_store,
            envelope_id=envelope_id,
            envelope_currency=envelope_currency,
            envelope_allowed_rails=envelope_allowed_rails,
            reference_currency=reference_currency,
            policy_engine=policy_engine,
            credential_store=credential_store,
            recoverer=recoverer,
        )
        self._auth = auth
        self._http = httpx.AsyncClient(auth=auth)

    # ------------------------------------------------------------------
    # Public namespace accessors
    # ------------------------------------------------------------------

    @property
    def envelopes(self) -> _EnvelopesNamespace:
        """Namespace for creating and managing spending envelopes.

        Requires ``trace_sink`` to be configured; methods raise ``RuntimeError``
        when called without one.
        """
        return self._envelopes

    # ------------------------------------------------------------------
    # Internal trace helper
    # ------------------------------------------------------------------

    async def _traced(self, coro: Any, ts_start: datetime) -> httpx.Response:
        """Execute an httpx coroutine and emit a passthrough trace if needed.

        The auth_flow marks paid responses with ``extensions["routeweiler_emitted"]``.
        Any response that does not carry that flag gets a passthrough trace here.
        Errors raised by auth_flow (RailNotSupportedError, SigningError, etc.) have
        already been traced by auth_flow, so we let them propagate without re-tracing.

        If split-URL recovery succeeded, the auth_flow stashes the recovered 2xx on
        ``extensions["routeweiler_recovered_response"]``.  We substitute it here so the
        caller receives the actual fulfilment response, not the original 4xx.
        """
        resp: httpx.Response = await coro
        # Substitute the recovered response if split-URL recovery succeeded.
        recovered: httpx.Response | None = resp.extensions.pop(
            "routeweiler_recovered_response", None
        )
        if recovered is not None:
            resp = recovered
        ts_end = datetime.now(UTC)
        if self._emitter and not resp.extensions.get("routeweiler_emitted"):
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
            if self._pending_envelope_spec is not None:
                spec = self._pending_envelope_spec
                await self._budget_store.create_envelope_if_absent(spec)
                currency = self._budget_store.get_envelope_currency_sync(spec.id)
                allowed_rails = self._budget_store.get_envelope_allowed_rails_sync(spec.id)
                if currency is None:  # pragma: no cover
                    raise RuntimeError(
                        f"Envelope '{spec.id}' was just created but currency could not be read."
                    )
                self._auth.bind_envelope(currency=currency, allowed_rails=allowed_rails)
                if self._emitter is not None:
                    self._emitter.bind_envelope_currency(currency)
        if self._credential_store is not None:
            await self._credential_store.start()
        if self._trace_sink is not None:
            await self._trace_sink.start()

    async def __aenter__(self) -> Routeweiler:
        await self._http.__aenter__()
        await self.start()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()
