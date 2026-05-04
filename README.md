# routewiler

The neutral micropayment router for autonomous agents. A single async HTTP client —
`await client.get(url)` — that transparently handles `402 Payment Required` across
x402 v2, L402 (Lightning), and Stripe MPP.

> **Async-only.** `Routewiler` wraps `httpx.AsyncClient`. All methods (`get`, `post`,
> …) are coroutines and must be awaited inside an `async` function.

## Install

```bash
pip install routewiler
```

## Quick start

```python
import asyncio
import os
from eth_account import Account
from routewiler import Routewiler, Funding

signer = Account.from_key(os.environ["PRIVATE_KEY"])

async def main():
    async with Routewiler(funding=[Funding.base_usdc(wallet=signer)]) as client:
        response = await client.get("https://api.example.com/data")
        print(response.json())

asyncio.run(main())
```

## SQLite trace recorder

Enable local tracing with `TraceSink.sqlite`. Every call (paid or free) produces
exactly one `TraceEvent` row, including the on-chain tx hash, FMV in your envelope
currency, and the outcome:

```python
from routewiler import Routewiler, Funding, TraceSink

async with Routewiler(
    funding=[Funding.base_usdc(wallet=signer)],
    trace_sink=TraceSink.sqlite("./routewiler-traces.db"),
) as client:
    response = await client.get("https://api.example.com/data")

# Inspect with the sqlite3 CLI:
# sqlite3 ./routewiler-traces.db \
#   'SELECT request_id, selected_rail, http_status, amount_envelope FROM trace_events;'
```

Tracing is disabled by default (`trace_sink=None`). Hosted trace upload and the
`url_mode="hash"` privacy option ship in a later release.

## Lightning (L402) payments

L402 payments require a running Lightning node. Routewiler supports any node
reachable over the LND gRPC interface — [LND](https://github.com/lightningnetwork/lnd),
[Voltage](https://voltage.cloud), and [Greenlight](https://blockstream.com/lightning/greenlight/)
all work. Pass `LightningFundingSource` (from `routewiler.funding.lightning`) with your
node's gRPC host, port, macaroon, and TLS cert. See `funding/lightning.py` for the full
API and `tests/rails/test_l402_e2e_polar.py` for a live integration test using Polar regtest.
