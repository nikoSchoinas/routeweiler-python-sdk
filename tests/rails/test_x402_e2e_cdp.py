"""Live x402 end-to-end test against the public x402.org Base-Sepolia facilitator.

SKIPPED by default. Run with:
    hatch run test-live tests/test_x402_e2e_cdp.py

Required environment variables:
    ROUTEWEILER_TEST_PRIVATE_KEY          Base-Sepolia wallet with ≥0.001 testnet USDC.
    ROUTEWEILER_TEST_MERCHANT_RECIPIENT   Base-Sepolia address that receives the 0.0001 USDC.
    ROUTEWEILER_TEST_FACILITATOR_URL      (optional) defaults to https://x402.org/facilitator

Funding a Base-Sepolia test wallet:
    1. ETH for gas: https://www.alchemy.com/faucets/ethereum-sepolia (bridge from Sepolia)
       or https://faucet.quicknode.com/base/sepolia
    2. Testnet USDC: https://faucet.circle.com/ (select Base Sepolia)

This test spins up an in-process Starlette merchant that:
    1. On first request returns 402 with a real PaymentRequirements for 0.0001 USDC
       on Base-Sepolia.
    2. On the signed retry POSTs to the facilitator /verify then /settle.
    3. Returns 200 with the real PAYMENT-RESPONSE containing an on-chain tx hash.

The test asserts that the trace row's proof_value is a valid 32-byte hex tx hash.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest
from eth_account import Account
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from routeweiler import Funding, Routeweiler
from routeweiler.trace.sink_sqlite import TraceSink

# ---------------------------------------------------------------------------
# Pytest marker
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.live

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_FACILITATOR = "https://www.x402.org/facilitator"
_PAYMENT_AMOUNT = "100"  # 0.0001 USDC in base units (6 decimals)
_ASSET = "0x036cbd53842c5426634e7929541ec2318f3dcf7e"  # USDC on Base-Sepolia

_HEX32_RE = re.compile(r"^0x[0-9a-f]{64}$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Live merchant factory
# ---------------------------------------------------------------------------


def _build_merchant_app(
    facilitator_url: str,
    recipient: str,
) -> Starlette:
    """Build a Starlette app that acts as a real x402 merchant.

    On GET /paid (no PAYMENT-SIGNATURE): returns 402 with a real challenge.
    On retry (has PAYMENT-SIGNATURE): POSTs to facilitator /verify + /settle,
        returns 200 with PAYMENT-RESPONSE or 402 if verify fails.
    """

    def _challenge_b64() -> str:
        data: dict[str, Any] = {
            "x402Version": 2,
            "accepts": [
                {
                    "scheme": "exact",
                    "network": "eip155:84532",
                    "amount": _PAYMENT_AMOUNT,
                    "description": "Live Routeweiler testnet smoke test",
                    "mimeType": "application/json",
                    "payTo": recipient,
                    "maxTimeoutSeconds": 120,
                    "asset": _ASSET,
                    "extra": {
                        "nonce": "0x" + os.urandom(32).hex(),
                        "validBefore": 9_999_999_999,
                        "validAfter": 0,
                        "name": "USDC",
                        "version": "2",
                    },
                }
            ],
        }
        return base64.b64encode(json.dumps(data).encode()).decode()

    challenge_b64 = _challenge_b64()

    async def paid(request: Request) -> Response:
        sig = request.headers.get("PAYMENT-SIGNATURE", "")
        if not sig:
            return Response(
                content=b"payment required",
                status_code=402,
                headers={"PAYMENT-REQUIRED": challenge_b64},
            )

        # Reconstruct payment requirements for the facilitator.
        challenge_data = json.loads(base64.b64decode(challenge_b64))
        req_body = {
            "paymentPayload": json.loads(base64.b64decode(sig)),
            "paymentRequirements": challenge_data["accepts"][0],
        }

        async with httpx.AsyncClient(timeout=30) as fc:
            verify_resp = await fc.post(f"{facilitator_url}/verify", json=req_body)
            if not verify_resp.is_success:
                return Response(
                    content=verify_resp.content,
                    status_code=402,
                    headers={"PAYMENT-REQUIRED": challenge_b64},
                )
            verify_data = verify_resp.json()
            if not verify_data.get("isValid"):
                return Response(
                    content=json.dumps(verify_data).encode(),
                    status_code=402,
                )

            settle_resp = await fc.post(f"{facilitator_url}/settle", json=req_body)
            settle_data = settle_resp.json()

        if not settle_data.get("success"):
            return Response(
                content=json.dumps(settle_data).encode(),
                status_code=402,
            )

        payment_response_b64 = base64.b64encode(json.dumps(settle_data).encode()).decode()
        return JSONResponse(
            {"result": "ok"},
            headers={"PAYMENT-RESPONSE": payment_response_b64},
        )

    return Starlette(routes=[Route("/paid", paid)])


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.live
async def test_x402_live_cdp_base_sepolia(tmp_path: Path) -> None:
    """Pay 0.0001 testnet USDC via x402.org Base-Sepolia facilitator; assert trace."""
    private_key = os.environ.get("ROUTEWEILER_TEST_PRIVATE_KEY")
    recipient = os.environ.get("ROUTEWEILER_TEST_MERCHANT_RECIPIENT")
    facilitator_url = os.environ.get("ROUTEWEILER_TEST_FACILITATOR_URL", _DEFAULT_FACILITATOR)

    if not private_key:
        pytest.skip("ROUTEWEILER_TEST_PRIVATE_KEY not set — cannot run live test.")
    if not recipient:
        pytest.skip("ROUTEWEILER_TEST_MERCHANT_RECIPIENT not set — cannot run live test.")

    wallet = Account.from_key(private_key)
    merchant_app = _build_merchant_app(facilitator_url, recipient)
    transport = httpx.ASGITransport(app=merchant_app)  # type: ignore[arg-type]

    db_path = tmp_path / "live-traces.db"
    sink = TraceSink.sqlite(db_path, url_mode="raw")
    client = Routeweiler(
        funding=[Funding.base_sepolia_usdc(wallet=wallet)],
        trace_sink=sink,
    )
    # Replace transport with the in-process merchant.
    client._http = httpx.AsyncClient(
        auth=client._http.auth,
        event_hooks=client._http.event_hooks,
        transport=transport,
    )

    resp = await client.get("http://testmerchant/paid")
    await client.aclose()

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    assert resp.json() == {"result": "ok"}

    # Check the trace row.
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM trace_events").fetchall()
    conn.close()
    assert len(rows) == 1, f"Expected 1 trace row, got {len(rows)}"

    row = dict(rows[0])
    payload = json.loads(row["payload"])

    assert row["selected_rail"] == "x402"
    assert row["http_status"] == 200
    assert row["service_delivered"] == 1

    proof = payload["payment"]["proofValue"]
    assert _HEX32_RE.match(proof), f"proof_value does not look like a 32-byte tx hash: {proof!r}"

    print(f"\n✓ Live x402 testnet payment confirmed. tx hash: {proof}")
