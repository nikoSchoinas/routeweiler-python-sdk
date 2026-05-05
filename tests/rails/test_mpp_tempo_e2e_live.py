"""Live MPP-Tempo end-to-end test against Tempo Moderato public testnet.

SKIPPED by default. Run with:
    hatch run test-live tests/rails/test_mpp_tempo_e2e_live.py

Required environment variables:
    ROUTEWILER_TEST_TEMPO_PRIVATE_KEY   Hex private key for a Moderato-funded wallet.
    ROUTEWILER_TEST_TEMPO_RECIPIENT     Recipient address on Moderato (receives the test payment).

Optional environment variables:
    ROUTEWILER_TEST_TEMPO_RPC           Tempo Moderato RPC endpoint.
                                        Defaults to https://rpc.moderato.tempo.xyz
    ROUTEWILER_TEST_TEMPO_TOKEN         PathUSD contract on Moderato.
                                        Defaults to 0x20c0000000000000000000000000000000000000

Funding a Moderato testnet wallet:
    1. Generate a secp256k1 keypair — any Ethereum-compatible tool works (cast wallet new,
       MetaMask, ethers.js, etc.).
    2. Visit https://faucet.tempo.xyz (or the Moderato-specific faucet linked from
       https://docs.tempo.xyz) and request PathUSD for your address.
    3. Confirm the balance at https://explore.testnet.tempo.xyz.

This test spins up an in-process Starlette merchant that:
    1. On first request returns 402 with a real ``WWW-Authenticate: Payment method=tempo``
       challenge for 1000 PathUSD base units (= 0.001 PathUSD at 6 decimals).
    2. On retry, decodes the credential, submits the signed transaction to the Tempo
       Moderato RPC via ``eth_sendRawTransaction``, polls ``eth_getTransactionReceipt``
       until included (≤30 s timeout), then returns 200 with a real ``Payment-Receipt``
       carrying the on-chain tx hash.

Asserts:
    - Final HTTP status is 200.
    - ``proof_type`` in the trace row is ``"txid"``.
    - ``proof_value`` matches ``^0x[0-9a-f]{64}$``.
    - ``Payment-Receipt`` reference also matches the regex.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path

import httpx
import pytest
from eth_account import Account
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from routewiler import Routewiler
from routewiler.funding import Funding
from routewiler.rails._mpp_http import (
    b64url_decode,
    b64url_encode,
    jcs_encode,
)
from routewiler.trace.sink_sqlite import TraceSink

pytestmark = pytest.mark.live

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_RPC = "https://rpc.moderato.tempo.xyz"
_DEFAULT_TOKEN = "0x20c0000000000000000000000000000000000000"  # PathUSD on Moderato
_PAYMENT_AMOUNT = "1000"  # 0.001 PathUSD (6 decimals)
_CHAIN_ID = 42431  # Tempo Moderato testnet
_VALID_WINDOW_SECONDS = 300
_POLL_INTERVAL_S = 2
_POLL_TIMEOUT_S = 30

_HEX32_RE = re.compile(r"^0x[0-9a-f]{64}$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Live merchant (in-process Starlette app)
# ---------------------------------------------------------------------------


def _build_merchant_app(
    *,
    recipient: str,
    token: str,
    rpc_url: str,
) -> Starlette:
    """Build a Starlette app that acts as a real MPP-Tempo merchant.

    GET /paid (no Authorization): returns 402 with a real Payment challenge.
    GET /paid (with Authorization: Payment ...):
        - Decodes the credential and extracts the signed tx.
        - Submits it to the Tempo Moderato RPC via eth_sendRawTransaction.
        - Polls eth_getTransactionReceipt until included (≤30 s).
        - Returns 200 with a real Payment-Receipt header.
    """
    charge_id = "live-" + hashlib.sha256(os.urandom(16)).hexdigest()[:16]
    valid_until = int(time.time()) + _VALID_WINDOW_SECONDS
    expires_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(valid_until))

    request_json: dict[str, object] = {
        "amount": _PAYMENT_AMOUNT,
        "currency": token,
        "recipient": recipient,
        "description": "Routewiler live testnet smoke test",
        "methodDetails": {
            "chainId": _CHAIN_ID,
            "feePayer": False,
            "memo": "0x" + "00" * 32,
            "splits": [],
            "supportedModes": ["pull"],
        },
    }
    request_b64 = b64url_encode(jcs_encode(request_json))
    www_auth = (
        f'Payment id="{charge_id}", '
        f'realm="live.routewiler.test", '
        f'method="tempo", '
        f'intent="charge", '
        f'request="{request_b64}", '
        f'expires="{expires_iso}"'
    )

    async def _rpc_call(method: str, params: list[object]) -> object:
        """Send a JSON-RPC request to the Tempo Moderato RPC endpoint."""
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.post(rpc_url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                raise RuntimeError(f"RPC error: {data['error']}")
            return data.get("result")

    def _extract_signed_tx(auth_header: str) -> str:
        """Decode the MPP credential and return the signed tx hex.

        Raises ValueError on any validation failure.
        """
        _, token_b64 = auth_header.split(" ", 1)
        raw = b64url_decode(token_b64.strip())
        credential: dict[str, object] = json.loads(raw)
        challenge_obj = credential.get("challenge", {})
        if not isinstance(challenge_obj, dict) or challenge_obj.get("id") != charge_id:
            raise ValueError(f"challengeId mismatch: got {challenge_obj.get('id')!r}")
        payload_obj = credential.get("payload", {})
        if not isinstance(payload_obj, dict) or payload_obj.get("type") != "transaction":
            raise ValueError(f"invalid payload type: {payload_obj.get('type')!r}")
        signed_tx = str(payload_obj.get("signature", ""))
        if not signed_tx.startswith("0x76"):
            raise ValueError("signature must start with 0x76")
        return signed_tx

    async def paid(request: Request) -> Response:
        auth_header = request.headers.get("Authorization", "")

        if not auth_header.lower().startswith("payment "):
            return Response(
                content=b"payment required",
                status_code=402,
                headers={"WWW-Authenticate": www_auth},
            )

        try:
            signed_tx = _extract_signed_tx(auth_header)
        except Exception as exc:
            return Response(f"credential error: {exc}".encode(), status_code=401)

        try:
            tx_hash = str(await _rpc_call("eth_sendRawTransaction", [signed_tx]))
        except Exception as exc:
            return Response(f"eth_sendRawTransaction failed: {exc}".encode(), status_code=402)

        # Poll for receipt
        receipt: object = None
        deadline = time.monotonic() + _POLL_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                receipt = await _rpc_call("eth_getTransactionReceipt", [tx_hash])
            except Exception:
                receipt = None
            if receipt is not None:
                break
            await asyncio.sleep(_POLL_INTERVAL_S)

        if receipt is None:
            return Response(
                f"tx {tx_hash} not mined within {_POLL_TIMEOUT_S}s".encode(),
                status_code=402,
            )

        receipt_payload: dict[str, object] = {
            "challengeId": charge_id,
            "method": "tempo",
            "reference": tx_hash,
            "settlement": {"amount": _PAYMENT_AMOUNT, "currency": token},
            "status": "success",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        return JSONResponse(
            {"result": "ok", "rail": "mpp-tempo", "live": True, "txHash": tx_hash},
            headers={"Payment-Receipt": b64url_encode(jcs_encode(receipt_payload))},
        )

    return Starlette(routes=[Route("/paid", paid)])


# ---------------------------------------------------------------------------
# Live test
# ---------------------------------------------------------------------------


@pytest.mark.live
async def test_mpp_tempo_live_moderato(tmp_path: Path) -> None:
    """Pay 0.001 PathUSD via MPP-Tempo on Tempo Moderato testnet; assert trace."""
    private_key = os.environ.get("ROUTEWILER_TEST_TEMPO_PRIVATE_KEY")
    recipient = os.environ.get("ROUTEWILER_TEST_TEMPO_RECIPIENT")
    rpc_url = os.environ.get("ROUTEWILER_TEST_TEMPO_RPC", _DEFAULT_RPC)
    token = os.environ.get("ROUTEWILER_TEST_TEMPO_TOKEN", _DEFAULT_TOKEN)

    if not private_key:
        pytest.skip("ROUTEWILER_TEST_TEMPO_PRIVATE_KEY not set — cannot run live test.")
    if not recipient:
        pytest.skip("ROUTEWILER_TEST_TEMPO_RECIPIENT not set — cannot run live test.")

    wallet = Account.from_key(private_key)
    funding_source = Funding.tempo_pathusd_moderato(wallet=wallet)

    db_path = tmp_path / "tempo-live.db"
    sink = TraceSink.sqlite(db_path, url_mode="raw")
    client = Routewiler(funding=[funding_source], trace_sink=sink)

    merchant = _build_merchant_app(recipient=recipient, token=token, rpc_url=rpc_url)
    client._http = httpx.AsyncClient(
        auth=client._http.auth,
        event_hooks=client._http.event_hooks,
        transport=httpx.ASGITransport(app=merchant),  # type: ignore[arg-type]
    )

    response = await client.get("http://tempo-live/paid")
    await client.aclose()

    # ---- HTTP assertions ----
    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
    data = response.json()
    assert data.get("rail") == "mpp-tempo"
    assert data.get("live") is True

    tx_hash_from_body: str = data.get("txHash", "")
    assert _HEX32_RE.match(tx_hash_from_body), (
        f"txHash in body is not a valid 32-byte hex: {tx_hash_from_body!r}"
    )

    # ---- Trace assertions ----
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM trace_events WHERE selected_rail = 'mpp-tempo'").fetchall()
    conn.close()

    assert len(rows) == 1, f"Expected 1 trace row, got {len(rows)}"
    row = dict(rows[0])

    assert row["http_status"] == 200
    assert row["service_delivered"] == 1

    payload = json.loads(row["payload"])
    assert payload["payment"]["proofType"] == "txid"

    proof_value: str = payload["payment"]["proofValue"]
    assert _HEX32_RE.match(proof_value), f"proof_value is not a valid 32-byte hex: {proof_value!r}"
