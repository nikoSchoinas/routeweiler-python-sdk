"""In-process ASGI mock L402 server that mimics the Refined Element split-URL scenario.

Exposes three routes:
    GET /checkout/{order_id}  — 402 on first visit (no auth), 404 on valid L402 retry.
    GET /orders/{order_id}/fulfil — 200 on valid L402 Authorization.

The 404 from /checkout/{id} after payment mirrors the incident described at:
  https://refinedelement.com/blog/l402-broke-at-the-worst-possible-moment-here-s-what-we-learned

A naive client would stop here; Routewiler's split-URL recovery consults the
lightning-shop manifest and retries at /orders/{id}/fulfil with the same credential.
"""

from __future__ import annotations

import hashlib

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from tests.fixtures.l402_mock_server import (
    MOCK_MACAROON_B64,
    MOCK_PAYMENT_HASH,
    MOCK_WWW_AUTHENTICATE,
)

# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


def _validate_l402_header(request: Request) -> bool:
    """Return True when the Authorization: L402 header carries the expected credential."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.lower().startswith("l402 "):
        return False
    try:
        _, credential = auth_header.split(" ", 1)
        mac, preimage_hex = credential.rsplit(":", 1)
        preimage_bytes = bytes.fromhex(preimage_hex)
        actual_hash = hashlib.sha256(preimage_bytes).hexdigest()
        return actual_hash == MOCK_PAYMENT_HASH and mac == MOCK_MACAROON_B64
    except Exception:
        return False


async def checkout(request: Request) -> Response:
    """Checkout endpoint: 402 without auth; 404 with valid auth (the split-URL bug)."""
    order_id = request.path_params.get("order_id", "unknown")

    if not _validate_l402_header(request):
        return Response(
            content=b"payment required",
            status_code=402,
            headers={"WWW-Authenticate": MOCK_WWW_AUTHENTICATE},
        )

    # Client paid and retried with a valid L402 credential, but the fulfilment is
    # at a different URL — this is the exact Refined Element split-URL failure mode.
    return JSONResponse(
        {
            "error": "not_found",
            "hint": f"order {order_id!r} fulfilment is at /orders/{order_id}/fulfil",
        },
        status_code=404,
    )


async def fulfil(request: Request) -> Response:
    """Fulfilment endpoint: 200 for valid L402 credential, 401 otherwise."""
    order_id = request.path_params.get("order_id", "unknown")

    if not _validate_l402_header(request):
        return Response(
            content=b"missing or invalid L402 credential",
            status_code=401,
        )

    return JSONResponse({"order_id": order_id, "status": "fulfilled"})


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

mock_split_url_app = Starlette(
    routes=[
        Route("/checkout/{order_id}", checkout),
        Route("/orders/{order_id}/fulfil", fulfil),
    ]
)
