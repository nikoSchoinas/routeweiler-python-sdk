"""In-process ASGI mock MPP-Tempo server for integration testing.

Exposes two routes:
    GET /protected   — returns 402 on first visit; validates MPP credential
                       and returns 200 + Payment-Receipt on valid retry.
    GET /free        — always returns 200 (passthrough test helper).

Mounted via ``httpx.ASGITransport(app=mock_mpp_tempo_app)`` in test fixtures.
No subprocess, no port binding, no real Tempo chain.

The mock validates credential *structure* only (parses, challenge.id
round-trips, source DID has correct chainId prefix) — not the cryptographic
signature.  The cryptographic path is exercised by the live testnet test.
"""

from __future__ import annotations

import json

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from routeweiler.rails._mpp_http import (
    b64url_decode,
    b64url_encode,
    build_payment_receipt,
    jcs_encode,
)

# ---------------------------------------------------------------------------
# Deterministic test fixtures
# ---------------------------------------------------------------------------

MOCK_CHARGE_ID = "qB3wErTyU7iOpAsD9fGhJk"
MOCK_RECIPIENT = "0xRecipient" + "00" * 15
MOCK_AMOUNT = "10000"  # 0.01 PathUSD in base units (6 decimals)
MOCK_CHAIN_ID = 42431  # Tempo Moderato testnet
MOCK_TOKEN = "0x20c0000000000000000000000000000000000000"  # PathUSD on Moderato

# Synthetic signed-tx hash (not real keccak — deterministic for tests)
MOCK_TX_HASH = "0x" + "bb" * 32

MOCK_REQUEST_JSON: dict[str, object] = {
    "amount": MOCK_AMOUNT,
    "currency": MOCK_TOKEN,
    "recipient": MOCK_RECIPIENT,
    "description": "Test charge",
    "methodDetails": {
        "chainId": MOCK_CHAIN_ID,
        "feePayer": False,
        "memo": "0x" + "00" * 32,
        "splits": [],
        "supportedModes": ["pull"],
    },
}

# Base64url-encoded JCS-JSON of the request param
MOCK_REQUEST_B64 = b64url_encode(jcs_encode(MOCK_REQUEST_JSON))

MOCK_EXPIRES = "2099-12-31T23:59:59Z"

MOCK_WWW_AUTHENTICATE = (
    f'Payment id="{MOCK_CHARGE_ID}", '
    f'realm="mock.routeweiler.test", '
    f'method="tempo", '
    f'intent="charge", '
    f'request="{MOCK_REQUEST_B64}", '
    f'expires="{MOCK_EXPIRES}"'
)

# Pre-built receipt for the happy-path 200 response
MOCK_RECEIPT_HEADER = build_payment_receipt(
    challenge_id=MOCK_CHARGE_ID,
    method="tempo",
    reference=MOCK_TX_HASH,
    amount=MOCK_AMOUNT,
    currency=MOCK_TOKEN,
    status="success",
)


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def protected(request: Request) -> Response:
    auth_header = request.headers.get("Authorization", "")

    if not auth_header.lower().startswith("payment "):
        return Response(
            content=b"payment required",
            status_code=402,
            headers={"WWW-Authenticate": MOCK_WWW_AUTHENTICATE},
        )

    # Validate: Authorization: Payment <b64url(JCS-JSON)>
    try:
        _, token = auth_header.split(" ", 1)
        raw = b64url_decode(token.strip())
        credential: dict[str, object] = json.loads(raw)

        challenge_obj = credential.get("challenge", {})
        if not isinstance(challenge_obj, dict):
            raise ValueError("credential.challenge must be a dict")
        if challenge_obj.get("id") != MOCK_CHARGE_ID:
            raise ValueError(
                f"challengeId mismatch: got {challenge_obj.get('id')!r}, "
                f"expected {MOCK_CHARGE_ID!r}"
            )

        source = str(credential.get("source", ""))
        if not source.startswith(f"did:pkh:eip155:{MOCK_CHAIN_ID}:"):
            raise ValueError(f"source DID has unexpected prefix: {source!r}")

        payload_obj = credential.get("payload", {})
        if not isinstance(payload_obj, dict):
            raise ValueError("credential.payload must be a dict")
        if payload_obj.get("type") != "transaction":
            raise ValueError(f"payload.type must be 'transaction', got {payload_obj.get('type')!r}")
        if not str(payload_obj.get("signature", "")).startswith("0x76"):
            raise ValueError("payload.signature must start with 0x76")

    except Exception as exc:
        return Response(
            content=f"invalid MPP credential: {exc}".encode(),
            status_code=401,
        )

    return JSONResponse(
        {"result": "ok", "rail": "mpp-tempo"},
        headers={"Payment-Receipt": MOCK_RECEIPT_HEADER},
    )


async def free(request: Request) -> Response:
    return JSONResponse({"free": True})


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

mock_mpp_tempo_app = Starlette(
    routes=[
        Route("/protected", protected),
        Route("/free", free),
    ]
)
