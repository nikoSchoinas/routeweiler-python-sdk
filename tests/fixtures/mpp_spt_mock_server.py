"""In-process ASGI mock MPP-SPT server for integration testing.

Exposes two routes:
    GET /protected   — returns 402 on first visit; validates MPP-SPT credential
                       structure and returns 200 + Payment-Receipt on valid retry.
    GET /free        — always returns 200 (passthrough test helper).

Mounted via ``httpx.ASGITransport(app=mock_mpp_spt_app)`` in test fixtures.
No subprocess, no port binding, no real Stripe API.

The mock validates credential *structure* only:
  - ``payload.type == "shared_payment_granted_token"``
  - ``payload.id`` starts with ``"spt_test_"``
  - ``source`` starts with ``"stripe:customer:"``
  - ``challenge.id`` round-trips correctly

Cryptographic verification of the SPT is not performed here — that is the
Stripe API's job on the merchant side.
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

MOCK_CHARGE_ID = "K9pLmN3qR7sT5vW2xY4zA1b"
MOCK_RECIPIENT = "acct_1MOCKMERCHANT0000000"  # Stripe Connect account id
MOCK_AMOUNT = "500"  # $5.00 in cents
MOCK_CURRENCY = "usd"
MOCK_CUSTOMER = "cus_TESTBUYER000000000000"
MOCK_PAYMENT_METHOD = "pm_card_visa_test"

# Synthetic PaymentIntent id — this is what the merchant would return in
# Payment-Receipt.reference after redeeming the SPT.
MOCK_PAYMENT_INTENT = "pi_test_3MOCK_REDEEMED0000000000"

MOCK_REQUEST_JSON: dict[str, object] = {
    "amount": MOCK_AMOUNT,
    "currency": MOCK_CURRENCY,
    "recipient": MOCK_RECIPIENT,
    "description": "Test SPT charge",
    "methodDetails": {
        "paymentMethodHint": "card",
        "sellerDetails": {"account": MOCK_RECIPIENT},
    },
}

MOCK_REQUEST_B64 = b64url_encode(jcs_encode(MOCK_REQUEST_JSON))

MOCK_EXPIRES = "2099-12-31T23:59:59Z"

MOCK_WWW_AUTHENTICATE = (
    f'Payment id="{MOCK_CHARGE_ID}", '
    f'realm="mock.routeweiler.test", '
    f'method="stripe", '
    f'intent="charge", '
    f'request="{MOCK_REQUEST_B64}", '
    f'expires="{MOCK_EXPIRES}"'
)

# Pre-built receipt for the happy-path 200 response
MOCK_RECEIPT_HEADER = build_payment_receipt(
    challenge_id=MOCK_CHARGE_ID,
    method="stripe",
    reference=MOCK_PAYMENT_INTENT,
    amount=MOCK_AMOUNT,
    currency=MOCK_CURRENCY,
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
        if not source.startswith("stripe:customer:"):
            raise ValueError(f"source must start with 'stripe:customer:', got {source!r}")

        payload_obj = credential.get("payload", {})
        if not isinstance(payload_obj, dict):
            raise ValueError("credential.payload must be a dict")
        if payload_obj.get("type") != "shared_payment_granted_token":
            raise ValueError(
                f"payload.type must be 'shared_payment_granted_token', "
                f"got {payload_obj.get('type')!r}"
            )
        spt_id = str(payload_obj.get("id", ""))
        if not spt_id.startswith("spt_test_"):
            raise ValueError(f"payload.id must start with 'spt_test_', got {spt_id!r}")

    except Exception as exc:
        return Response(
            content=f"invalid MPP-SPT credential: {exc}".encode(),
            status_code=401,
        )

    return JSONResponse(
        {"result": "ok", "rail": "mpp-spt"},
        headers={"Payment-Receipt": MOCK_RECEIPT_HEADER},
    )


async def free(request: Request) -> Response:
    return JSONResponse({"free": True})


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

mock_mpp_spt_app = Starlette(
    routes=[
        Route("/protected", protected),
        Route("/free", free),
    ]
)
