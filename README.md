# routeweiler

<p align="center">
  <img src="https://raw.githubusercontent.com/nikoSchoinas/routeweiler-python-sdk/main/assets/routeweiler.png" alt="Routeweiler" width="720">
</p>

<p align="center">
  <a href="https://github.com/nikoSchoinas/routeweiler-python-sdk/actions/workflows/ci.yml?query=branch%3Amain"><img src="https://img.shields.io/github/actions/workflow/status/nikoSchoinas/routeweiler-python-sdk/ci.yml?branch=main&style=for-the-badge&label=build" alt="Build status"></a>
  <a href="https://pypi.org/project/routeweiler/"><img src="https://img.shields.io/pypi/v/routeweiler?style=for-the-badge" alt="PyPI version"></a>
  <a href="https://pypi.org/project/routeweiler/"><img src="https://img.shields.io/pypi/dm/routeweiler?style=for-the-badge" alt="PyPI downloads"></a>
  <a href="https://pypi.org/project/routeweiler/"><img src="https://img.shields.io/pypi/pyversions/routeweiler?style=for-the-badge&logo=python&logoColor=white" alt="Python versions"></a>
  <a href="https://codecov.io/gh/nikoSchoinas/routeweiler-python-sdk"><img src="https://img.shields.io/codecov/c/github/nikoSchoinas/routeweiler-python-sdk?style=for-the-badge" alt="Coverage"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache_2.0-blue?style=for-the-badge" alt="License: Apache-2.0"></a>
  <img src="https://img.shields.io/badge/status-beta-blue?style=for-the-badge" alt="Status: Beta">
  <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/badge/code%20style-ruff-261230?style=for-the-badge" alt="Code style: Ruff"></a>
  <img src="https://img.shields.io/badge/type%20checked-mypy-1f5082?style=for-the-badge" alt="Type-checked with mypy">
</p>

The neutral micropayment router for autonomous agents. A single async HTTP client —
`await routeweiler.get(url)` — that transparently handles `402 Payment Required` across
x402 (EVM), L402 (Lightning), MPP-Tempo (stablecoin), and MPP-SPT (Stripe).

**Documentation:** [docs.routeweiler.com](https://docs.routeweiler.com)

<p align="center">
  <img src="https://raw.githubusercontent.com/nikoSchoinas/routeweiler-python-sdk/main/assets/diagram.png" alt="Routeweiler flow diagram" width="900">
</p>

## Install

```bash
pip install routeweiler
```

Python 3.11+ required.

## Quick start

```python
import asyncio
import os
from eth_account import Account
from routeweiler import Routeweiler, Funding

signer = Account.from_key(os.environ["PRIVATE_KEY"])

async def main():
    async with Routeweiler(funding=[Funding.base_usdc(wallet=signer)]) as client:
        response = await client.get("https://api.example.com/data")
        print(response.json())

asyncio.run(main())
```

## Supported rails

| Rail | Method | Funding source | Networks |
|------|--------|---------------|----------|
| [x402](https://x402.org) | EVM signed transaction | `EvmFundingSource` | Base, Base-Sepolia |
| [L402](https://docs.lightning.engineering/the-lightning-network/l402) | BOLT-11 Lightning invoice | `LightningFundingSource` | Bitcoin, Regtest |
| [MPP-Tempo](https://paymentauth.org) | Tempo 0x76 stablecoin tx | `TempoFundingSource` | Moderato testnet |
| [MPP-SPT](https://docs.stripe.com/agentic-commerce) | Stripe Shared Payment Token | `StripeFundingSource` | USD, EUR, GBP |

## Funding sources

Each rail accepts a typed `FundingSource` holding the credentials it uses to sign or authorise payments. Key material stays in your process; Routeweiler never transmits private keys.

### x402 — `EvmFundingSource`

An `eth_account.LocalAccount` that signs EIP-3009 `transferWithAuthorization` for USDC on Base.

```python
from eth_account import Account
from routeweiler import Funding

wallet = Account.from_key(os.environ["PRIVATE_KEY"])  # 64-char hex secp256k1 key, 0x prefix optional
funding = Funding.base_usdc(wallet=wallet)            # mainnet (chain 8453)
# funding = Funding.base_sepolia_usdc(wallet=wallet)  # testnet (chain 84532)
```

### L402 — `LightningFundingSource`

Wraps an LND gRPC client. Bring a running LND node and pass its admin macaroon + TLS cert; Routeweiler pays BOLT-11 invoices through it.

```python
from routeweiler.funding.lightning import LightningFundingSource, LndClient

lnd = LndClient(
    grpc_host="localhost",
    grpc_port=10009,
    macaroon_path="/path/to/admin.macaroon",
    tls_cert_path="/path/to/tls.cert",
)
funding = await LightningFundingSource.create(lnd, network="bitcoin")
# network: "bitcoin" | "bitcoin-testnet" | "bitcoin-regtest" | "bitcoin-signet"
```

### MPP-Tempo — `TempoFundingSource`

Signs Tempo's type-0x76 charge transactions with an `eth_account.LocalAccount` — same key shape as x402, different chain. The same wallet can fund both rails.

```python
from eth_account import Account
from routeweiler import Funding

wallet = Account.from_key(os.environ["PRIVATE_KEY"])  # 64-char hex secp256k1 key, 0x prefix optional
funding = Funding.tempo_usdc(wallet=wallet)              # mainnet, USDC
# funding = Funding.tempo_pathusd_moderato(wallet=wallet)  # testnet, pathUSD
```

### MPP-SPT — `StripeFundingSource`

No on-device signing. Stripe holds the card; you supply a secret API key, a customer id, and a saved payment method id. Routeweiler asks Stripe to mint a Shared Payment Token at pay-time.

```python
from routeweiler import Funding

funding = Funding.stripe(
    api_key=os.environ["STRIPE_API_KEY"],   # sk_live_... / sk_test_...
    customer="cus_ABC123",                  # buyer's Stripe customer id
    payment_method="pm_XYZ789",             # saved card / bank
    currency="usd",                         # ISO-4217: usd | eur | gbp | ...
)
```

Pass any combination to `Routeweiler(funding=[...])`. The router picks the best rail per challenge based on [Policy](#policy) and what the server accepts.

## SQLite trace recorder

Enable local tracing with `TraceSink.sqlite`. Every call (paid or free) produces
exactly one `TraceEvent` row, including the on-chain tx hash and the payment outcome:

```python
from routeweiler import Routeweiler, Funding, TraceSink

async with Routeweiler(
    funding=[Funding.base_usdc(wallet=signer)],
    trace_sink=TraceSink.sqlite("./routeweiler.db"),
) as client:
    response = await client.get("https://api.example.com/data")

# Inspect with the sqlite3 CLI:
# sqlite3 ./routeweiler.db \
#   'SELECT request_id, selected_rail, http_status FROM trace_events;'
```

## Budget envelopes

Enforce per-session or per-agent spend caps with local SQLite budget envelopes.
Without a `budget_envelope`, tracing still works but **no cap is enforced**.

```python
from routeweiler import BudgetEnvelope, Funding, Routeweiler, TraceSink

async with Routeweiler(
    funding=[Funding.base_usdc(wallet=signer)],
    trace_sink=TraceSink.sqlite("routeweiler.db"),
    budget_envelope=BudgetEnvelope(
        id="session-abc",
        cap_minor_units=500,           # 5.00 USD (in cents)
        cap_currency="usd",
        allowed_rails=["x402", "l402"],
        ttl_seconds=3_600,             # 1 hour
    ),
) as client:
    response = await client.get("https://api.example.com/data")
```

`budget_envelope` accepts three forms:

- **`None`** (default) — no cap enforcement.
- **`str`** — ID of a pre-existing envelope; raises `EnvelopeNotFoundError` at
  construction time if the row is missing.
- **`BudgetEnvelope`** — declarative spec; If an envelope with the same `id` already exists it is reused unchanged.

Envelopes track reserved and settled amounts with Ed25519-signed draw receipts.
`BudgetExceededError` is raised if a payment would breach the cap.

## Policy

Control which rails are used, set per-call spend limits, or deny specific URLs:

```python
from routeweiler import Policy, PolicyRule, RuleMatch, Routeweiler

async with Routeweiler(
    funding=[...],
    policy=Policy(
        currency="usd",          # reference currency for max_per_call_minor_units
        rules=[
            PolicyRule(
                name="deny analytics",
                when=RuleMatch(url_matches="*.tracking.io"),
                deny=True,
            ),
            PolicyRule(
                name="cap per call",
                when=RuleMatch(url_matches="*"),
                max_per_call_minor_units=500,  # 5 USD cents
            ),
        ]
    ),
) as client:
    ...
```

`max_per_call_minor_units` requires a reference currency to compare rail-native quotes
against. Set `Policy(currency="usd")` (or any supported currency) when no
`budget_envelope` is configured. The envelope's `cap_currency` takes precedence when
both are present. If neither is provided and a rule uses `max_per_call_minor_units`,
`Routeweiler` raises `ValueError` at construction time.

## Releases

Releases follow [SemVer](https://semver.org). Pre-1.0 minors (`0.1.0 → 0.2.0`) may include breaking changes.

| Tag format | Channel | Install |
|---|---|---|
| `v0.2.0` | Stable | `pip install routeweiler` |
| `v0.2.0b1` | Beta | `pip install --pre routeweiler` |

A release is a git tag, not a merge. Merges to `main` run CI but don't publish. Pushing `python/vX.Y.Z` (or `python/vX.Y.ZbN`) to the monorepo triggers the release workflow, which builds the wheel + sdist, mirrors the tag as `vX.Y.Z` to this repo, attaches artefacts to the GitHub Release here, and — once the package is public — publishes to PyPI.

## License

Apache 2.0 — see [LICENSE](../../LICENSE).
