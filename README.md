# routeweiler

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

Enforce per-session or per-agent spend caps with local SQLite budget envelopes:

```python
async with Routeweiler(
    funding=[Funding.base_usdc(wallet=signer)],
    trace_sink=TraceSink.sqlite("routeweiler.db"),
    envelope_id="session-abc",
) as client:
    response = await client.get("https://api.example.com/data")
```

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
