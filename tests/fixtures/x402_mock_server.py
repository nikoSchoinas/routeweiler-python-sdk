"""In-process ASGI mock x402 server for integration testing.

Exposes two routes:
    GET /protected   — returns 402 on first visit; 200 + PAYMENT-RESPONSE on retry.
    GET /free        — always returns 200 (passthrough test helper).

Mounted via ``httpx.ASGITransport(app=mock_x402_app)`` in test fixtures.
No subprocess, no port binding, no on-chain settlement.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

# ---------------------------------------------------------------------------
# Challenge fixture — Base-Sepolia USDC, 1000 base units (0.001 USDC)
# ---------------------------------------------------------------------------
_CHALLENGE: dict[str, Any] = {
    "x402Version": 2,
    "accepts": [
        {
            "scheme": "exact",
            "network": "eip155:84532",
            "amount": "1000",
            "description": "Mock x402 endpoint",
            "mimeType": "application/json",
            "payTo": "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",  # Anvil account #0
            "maxTimeoutSeconds": 60,
            "asset": "0x036cbd53842c5426634e7929541ec2318f3dcf7e",  # USDC base-sepolia
            "extra": {
                "nonce": "0xdeadbeef00000000000000000000000000000000000000000000000000000001",
                "validBefore": 9_999_999_999,
                "validAfter": 0,
                "name": "USD Coin",
                "version": "2",
            },
        }
    ],
}

MOCK_TX_HASH = "0x" + "fa" * 32  # deterministic fake tx hash for assertions
MOCK_CHALLENGE_B64 = base64.b64encode(json.dumps(_CHALLENGE).encode()).decode()


def _settlement_b64(tx_hash: str = MOCK_TX_HASH) -> str:
    payload = {
        "success": True,
        "txHash": tx_hash,
        "networkId": "base-sepolia",
        "payerAddress": "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
        "amountPaid": "1000",
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


MOCK_SETTLEMENT_B64 = _settlement_b64()

# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def protected(request: Request) -> Response:
    sig_header = request.headers.get("PAYMENT-SIGNATURE", "")

    if not sig_header:
        return Response(
            content=b"payment required",
            status_code=402,
            headers={"PAYMENT-REQUIRED": MOCK_CHALLENGE_B64},
        )

    # Structural validation of the signed payload.
    try:
        outer = json.loads(base64.b64decode(sig_header))
        assert outer.get("x402Version") == 2, "missing x402Version"
        accepts_list = outer.get("payload")
        # The x402 SDK encodes the signed payment as a nested structure;
        # exact shape varies by SDK version.  We require at least that the
        # outer JSON decoded and has x402Version == 2.
        _ = accepts_list  # shape checked below when we have more SDK specifics
    except Exception as exc:
        return Response(
            content=f"bad PAYMENT-SIGNATURE: {exc}".encode(),
            status_code=400,
        )

    return JSONResponse(
        {"result": "ok"},
        headers={"PAYMENT-RESPONSE": MOCK_SETTLEMENT_B64},
    )


async def free(request: Request) -> Response:
    return JSONResponse({"free": True})


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

mock_x402_app = Starlette(
    routes=[
        Route("/protected", protected),
        Route("/free", free),
    ]
)
