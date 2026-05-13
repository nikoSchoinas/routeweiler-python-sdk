"""In-process ASGI mock server that mirrors the Lightning Enable Store split-URL pattern.

Documented in the Refined Element post-mortem:
  https://refinedelement.com/blog/l402-broke-at-the-worst-possible-moment-here-s-what-we-learned

Routes:
    POST /api/store/checkout  — 402 on first visit; 404 when Authorization: L402 is valid
                                (the split-URL bug: retry is refused at the checkout URL).
    POST /api/store/claim     — 200 when Authorization: L402 is valid; 401 otherwise.

The macaroon minted by /api/store/checkout is a real pymacaroons macaroon that embeds
``order_id=<MOCK_ORDER_ID>`` as a first-party caveat. Routeweiler's manifest recovery
extracts that caveat via the ``macaroon:order_id`` id_extractor, then replays the
credential at /api/store/claim — which accepts it and returns 200.
"""

from __future__ import annotations

import hashlib

from pymacaroons import Macaroon, Verifier  # type: ignore[import-untyped]
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from tests.fixtures.l402_mock_server import (
    MOCK_BOLT11,
    MOCK_PAYMENT_HASH,
)

# ---------------------------------------------------------------------------
# Mock macaroon — a real pymacaroons macaroon with an order_id caveat
# ---------------------------------------------------------------------------

MOCK_ORDER_ID: str = "order_test_123"
_MOCK_MACAROON_KEY: bytes = b"mock-root-key-for-testing"

_raw_mac = Macaroon(
    location="http://store.lightningenable.com",
    identifier=MOCK_PAYMENT_HASH,
    key=_MOCK_MACAROON_KEY,
)
_raw_mac.add_first_party_caveat(f"order_id={MOCK_ORDER_ID}")
MOCK_MACAROON_B64: str = _raw_mac.serialize()

MOCK_WWW_AUTHENTICATE: str = f'L402 macaroon="{MOCK_MACAROON_B64}", invoice="{MOCK_BOLT11}"'

# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


def _validate_l402_header(request: Request) -> bool:
    """Return True when the Authorization: L402 header carries a valid credential."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.lower().startswith("l402 "):
        return False
    try:
        _, credential = auth_header.split(" ", 1)
        mac_b64, preimage_hex = credential.rsplit(":", 1)
        preimage_bytes = bytes.fromhex(preimage_hex)
        actual_hash = hashlib.sha256(preimage_bytes).hexdigest()
        if actual_hash != MOCK_PAYMENT_HASH:
            return False
        # Verify the macaroon signature with the root key.
        mac = Macaroon.deserialize(mac_b64)
        v = Verifier()
        v.satisfy_general(lambda _: True)  # caveat satisfaction not under test here
        return v.verify(mac, _MOCK_MACAROON_KEY)
    except Exception:
        return False


async def checkout(request: Request) -> Response:
    """Checkout endpoint: 402 without auth; 404 with valid auth (the split-URL bug)."""
    if not _validate_l402_header(request):
        return Response(
            content=b"payment required",
            status_code=402,
            headers={"WWW-Authenticate": MOCK_WWW_AUTHENTICATE},
        )

    # Client paid and retried with a valid credential but the fulfilment is at a
    # different URL — the exact Lightning Enable Store failure mode.
    return JSONResponse(
        {"error": "not_found", "hint": "fulfilment is at /api/store/claim"},
        status_code=404,
    )


async def claim(request: Request) -> Response:
    """Fulfilment endpoint: 200 for valid L402 credential, 401 otherwise."""
    if not _validate_l402_header(request):
        return Response(content=b"missing or invalid L402 credential", status_code=401)

    return JSONResponse({"order_id": MOCK_ORDER_ID, "status": "fulfilled"})


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

mock_split_url_app = Starlette(
    routes=[
        Route("/api/store/checkout", checkout, methods=["POST"]),
        Route("/api/store/claim", claim, methods=["POST"]),
    ]
)
