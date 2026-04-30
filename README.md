# routewiler

The neutral micropayment router for autonomous agents. A single HTTP client —
`await client.get(url)` — that transparently handles `402 Payment Required` across
x402 v2, L402 (Lightning), and Stripe MPP.

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
