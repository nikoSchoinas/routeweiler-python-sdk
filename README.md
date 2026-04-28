# routewiler

The neutral micropayment router for autonomous agents. A single HTTP client —
`routewiler.get(url)` — that transparently handles `402 Payment Required` across
x402 v2, L402 (Lightning), and Stripe MPP.

## Install

```bash
pip install routewiler
```

## Quick start

```python
import routewiler

client = routewiler.Routewiler(
    envelope_id="env_01HW...",
    # funding and policy configured here in later releases
)

response = client.get("https://api.example.com/data")
print(response.json())
```
