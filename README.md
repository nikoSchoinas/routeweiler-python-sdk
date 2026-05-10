# routeweiler

<p align="center">
  <img src="assets/routweiler.png" alt="Routeweiler" width="480">
</p>

<p align="center">
  <a href="https://github.com/nikoSchoinas/routeweiler-python-sdk/actions/workflows/ci.yml?query=branch%3Amain"><img src="https://img.shields.io/github/actions/workflow/status/nikoSchoinas/routeweiler-python-sdk/ci.yml?branch=main&style=for-the-badge&label=build" alt="Build status"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache_2.0-blue?style=for-the-badge" alt="License: Apache-2.0"></a>
  <img src="https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.11 | 3.12 | 3.13">
  <img src="https://img.shields.io/badge/status-alpha-orange?style=for-the-badge" alt="Status: Alpha">
  <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/badge/code%20style-ruff-261230?style=for-the-badge" alt="Code style: Ruff"></a>
  <img src="https://img.shields.io/badge/type%20checked-mypy-1f5082?style=for-the-badge" alt="Type-checked with mypy">
</p>

**Status:** Alpha — `0.1.0.dev0` — API may change before 1.0.

The neutral micropayment router for autonomous agents. A single async HTTP client —
`await client.get(url)` — that transparently handles `402 Payment Required` across
x402, L402 (Lightning), MPP-Tempo (stablecoin), and MPP-SPT (Stripe).

> **Async-only.** `Routeweiler` wraps `httpx.AsyncClient`. All methods (`get`, `post`,
> …) are coroutines and must be awaited inside an `async` function.

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

## SQLite trace recorder

Enable local tracing with `TraceSink.sqlite`. Every call (paid or free) produces
exactly one `TraceEvent` row, including the on-chain tx hash, FMV in your envelope
currency, and the payment outcome:

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

**Default envelope.** When you enable `trace_sink`, a `"default"` envelope is
created automatically on first run: $100 USD cap, x402 rail only, 30-day TTL.

**Custom envelopes.** Pass a `BudgetEnvelopeSpec` as `budget_envelope`. The
client creates the envelope idempotently inside `async with` — no separate
construction step needed.

```python
from routeweiler import BudgetEnvelopeSpec, Funding, Routeweiler, TraceSink

async with Routeweiler(
    funding=[Funding.base_usdc(wallet=signer)],
    trace_sink=TraceSink.sqlite("routeweiler.db"),
    budget_envelope=BudgetEnvelopeSpec(
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

- **`None`** (default) — use the built-in `"default"` envelope.
- **`str`** — ID of a pre-existing envelope; raises `EnvelopeNotFoundError` at
  construction time if the row is missing.
- **`BudgetEnvelopeSpec`** — declarative spec; the envelope is created inside
  `__aenter__`. If an envelope with the same `id` already exists it is reused
  unchanged.

Envelopes track reserved and settled amounts with Ed25519-signed draw receipts.
`BudgetExceededError` is raised if a payment would breach the cap.

## Policy

Control which rails are used, set per-call spend limits, or deny specific URLs:

```python
from routeweiler import PolicyFile, Routeweiler

async with Routeweiler(
    funding=[...],
    policy=PolicyFile("policy.yaml"),
) as client:
    ...
```

```yaml
# policy.yaml
version: 1
rules:
  - name: "deny analytics"
    when:
      url_matches: "*.tracking.io"
    deny: true
  - name: "cap per call"
    max_per_call_minor_units: 500  # 5 USD cents
```

## License

Apache 2.0 — see [LICENSE](../../LICENSE).
