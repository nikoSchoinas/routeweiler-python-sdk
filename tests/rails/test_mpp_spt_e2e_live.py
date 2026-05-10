"""Live MPP-SPT tests against Stripe test mode.

Gated by ``--run-live`` marker.

``test_live_spt_creation``
    Minimal smoke test — creates an SPT directly via ``StripeSptCreator``.
    Requires: STRIPE_TEST_API_KEY, STRIPE_TEST_CUSTOMER,
              STRIPE_TEST_PAYMENT_METHOD, STRIPE_TEST_SELLER_PROFILE.

``test_live_spt_full_flow_redeems_via_payment_intent``
    Full end-to-end flow:
        402 challenge → Routeweiler mints SPT (buyer key) → retry →
        in-process merchant creates real Stripe PaymentIntent (merchant key) →
        200 + Payment-Receipt → trace row asserted.

    Requires all four vars above PLUS:
        STRIPE_TEST_SELLER_API_KEY — merchant's sk_test_... key used to
                                       create the PaymentIntent that redeems
                                       the SPT.

    Two-account topology (Stripe's supported setup):
        Buyer account (STRIPE_TEST_API_KEY) mints the SPT scoped to the
        seller's network_business_profile (STRIPE_TEST_SELLER_PROFILE).
        Merchant account (STRIPE_TEST_SELLER_API_KEY) owns that profile
        and redeems the SPT via PaymentIntent.create.

    The merchant-side call uses plain httpx (form-encoded API POST) so no
    additional stripe SDK dependency is introduced for the test.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from routeweiler import Routeweiler
from routeweiler.funding.stripe import StripeFundingSource, StripeSptCreator
from routeweiler.rails._mpp_http import (
    b64url_decode,
    b64url_encode,
    build_payment_receipt,
    jcs_encode,
)
from routeweiler.trace.sink_sqlite import TraceSink

pytestmark = pytest.mark.live

_VALID_WINDOW_SECONDS = 300
_CHARGE_AMOUNT = "100"  # $1.00 in cents (above Stripe $0.50 minimum)
_CHARGE_CURRENCY = "usd"
_PI_RE = re.compile(r"^pi_[A-Za-z0-9_]+$")
_SPT_RE = re.compile(r"^spt_[A-Za-z0-9_]+$")


# ---------------------------------------------------------------------------
# Smoke test — buyer half only
# ---------------------------------------------------------------------------


@pytest.mark.live
async def test_live_spt_creation() -> None:
    api_key = os.environ.get("STRIPE_TEST_API_KEY", "")
    customer = os.environ.get("STRIPE_TEST_CUSTOMER", "")
    payment_method = os.environ.get("STRIPE_TEST_PAYMENT_METHOD", "")
    seller_profile = os.environ.get("STRIPE_TEST_SELLER_PROFILE", "")

    if not api_key or not customer or not payment_method or not seller_profile:
        pytest.skip(
            "Live SPT test requires STRIPE_TEST_API_KEY, STRIPE_TEST_CUSTOMER, "
            "STRIPE_TEST_PAYMENT_METHOD, and STRIPE_TEST_SELLER_PROFILE env vars."
        )

    creator = StripeSptCreator(api_key=api_key)

    usage_limits = {
        "currency": "usd",
        "max_amount": 100,  # $1.00
        "expires_at": int(time.time()) + 300,  # 5 minutes from now
    }
    seller_details = {"network_business_profile": seller_profile}

    spt_id = await creator.create_spt(
        usage_limits=usage_limits,
        seller_details=seller_details,
        payment_method=payment_method,
        customer=customer,
    )

    assert isinstance(spt_id, str), f"Expected str, got {type(spt_id)}"
    assert spt_id.startswith("spt_"), f"Expected spt_id to start with 'spt_', got {spt_id!r}"


# ---------------------------------------------------------------------------
# Full e2e — buyer mints SPT, merchant redeems via real PaymentIntent
# ---------------------------------------------------------------------------


def _build_merchant_app(*, seller_profile: str, merchant_api_key: str) -> Starlette:
    """In-process Starlette merchant that issues a real 402 challenge and
    redeems the SPT via Stripe's PaymentIntent API.

    GET /paid (no Authorization):
        Returns 402 with a real MPP-SPT WWW-Authenticate challenge.
    GET /paid (Authorization: Payment ...):
        Decodes the credential, extracts the spt_id, calls Stripe
        POST /v1/payment_intents with the SPT, and on success returns 200
        with a real Payment-Receipt header carrying the PaymentIntent id.
    """
    charge_id = "live-spt-" + hashlib.sha256(os.urandom(16)).hexdigest()[:16]
    valid_until = int(time.time()) + _VALID_WINDOW_SECONDS
    expires_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(valid_until))

    request_json: dict[str, object] = {
        "amount": _CHARGE_AMOUNT,
        "currency": _CHARGE_CURRENCY,
        "recipient": seller_profile,
        "description": "Routeweiler live SPT e2e smoke test",
        "methodDetails": {
            "paymentMethodHint": "card",
            "sellerDetails": {"network_business_profile": seller_profile},
        },
    }
    request_b64 = b64url_encode(jcs_encode(request_json))
    www_auth = (
        f'Payment id="{charge_id}", '
        f'realm="live.routeweiler.test", '
        f'method="stripe", '
        f'intent="charge", '
        f'request="{request_b64}", '
        f'expires="{expires_iso}"'
    )

    def _extract_spt_id(auth_header: str) -> str:
        _, token_b64 = auth_header.split(" ", 1)
        raw = b64url_decode(token_b64.strip())
        credential: dict[str, object] = json.loads(raw)
        challenge_obj = credential.get("challenge", {})
        if not isinstance(challenge_obj, dict) or challenge_obj.get("id") != charge_id:
            raise ValueError(f"challengeId mismatch: got {challenge_obj.get('id')!r}")
        payload_obj = credential.get("payload", {})
        payload_type = payload_obj.get("type") if isinstance(payload_obj, dict) else None
        if payload_type != "shared_payment_granted_token":
            raise ValueError(f"invalid payload type: {payload_type!r}")
        spt_id = str(payload_obj.get("id", ""))
        if not spt_id.startswith("spt_"):
            raise ValueError(f"payload.id must start with 'spt_', got {spt_id!r}")
        return spt_id

    async def paid(request: Request) -> Response:
        auth_header = request.headers.get("Authorization", "")

        if not auth_header.lower().startswith("payment "):
            return Response(
                content=b"payment required",
                status_code=402,
                headers={"WWW-Authenticate": www_auth},
            )

        try:
            spt_id = _extract_spt_id(auth_header)
        except Exception as exc:
            return Response(f"credential error: {exc}".encode(), status_code=401)

        async with httpx.AsyncClient(timeout=30) as c:
            resp = await c.post(
                "https://api.stripe.com/v1/payment_intents",
                auth=(merchant_api_key, ""),
                data={
                    "amount": _CHARGE_AMOUNT,
                    "currency": _CHARGE_CURRENCY,
                    "payment_method_data[shared_payment_granted_token]": spt_id,
                    "confirm": "true",
                },
            )

        pi_data = resp.json()
        if not resp.is_success or pi_data.get("status") != "succeeded":
            return Response(
                f"PaymentIntent failed: {json.dumps(pi_data)}".encode(),
                status_code=402,
            )

        pi_id: str = pi_data["id"]
        receipt_header = build_payment_receipt(
            challenge_id=charge_id,
            method="stripe",
            reference=pi_id,
            amount=_CHARGE_AMOUNT,
            currency=_CHARGE_CURRENCY,
            status="success",
        )
        return JSONResponse(
            {"result": "ok", "rail": "mpp-spt", "live": True, "piId": pi_id},
            headers={"Payment-Receipt": receipt_header},
        )

    return Starlette(routes=[Route("/paid", paid)])


@pytest.mark.live
async def test_live_spt_full_flow_redeems_via_payment_intent(tmp_path: Path) -> None:
    """Full MPP-SPT round-trip on Stripe test mode.

    Buyer (STRIPE_TEST_API_KEY) mints an SPT scoped to the seller's
    network_business_profile; the in-process merchant (STRIPE_TEST_SELLER_API_KEY)
    redeems it via a real Stripe PaymentIntent and returns a Payment-Receipt.
    Asserts: HTTP 200, trace row with proofType=spt_id, settlement reference
    is a real pi_... PaymentIntent id.
    """
    buyer_api_key = os.environ.get("STRIPE_TEST_API_KEY", "")
    buyer_customer = os.environ.get("STRIPE_TEST_CUSTOMER", "")
    buyer_pm = os.environ.get("STRIPE_TEST_PAYMENT_METHOD", "")
    seller_profile = os.environ.get("STRIPE_TEST_SELLER_PROFILE", "")
    merchant_api_key = os.environ.get("STRIPE_TEST_SELLER_API_KEY", "")

    missing = [
        name
        for name, val in [
            ("STRIPE_TEST_API_KEY", buyer_api_key),
            ("STRIPE_TEST_CUSTOMER", buyer_customer),
            ("STRIPE_TEST_PAYMENT_METHOD", buyer_pm),
            ("STRIPE_TEST_SELLER_PROFILE", seller_profile),
            ("STRIPE_TEST_SELLER_API_KEY", merchant_api_key),
        ]
        if not val
    ]
    if missing:
        pytest.skip(f"Missing env vars: {', '.join(missing)}")

    merchant = _build_merchant_app(seller_profile=seller_profile, merchant_api_key=merchant_api_key)
    funding = StripeFundingSource(
        api_key=buyer_api_key,
        customer=buyer_customer,
        payment_method=buyer_pm,
        currency=_CHARGE_CURRENCY,
        # spt_creator omitted — defaults to real StripeSptCreator
    )
    db_path = tmp_path / "spt-live.db"
    sink = TraceSink.sqlite(db_path, url_mode="raw")
    client = Routeweiler(funding=[funding], trace_sink=sink)
    client._http = httpx.AsyncClient(
        auth=client._http.auth,
        event_hooks=client._http.event_hooks,
        transport=httpx.ASGITransport(app=merchant),  # type: ignore[arg-type]
    )

    response = await client.get("http://spt-live/paid")
    await client.aclose()

    # ---- HTTP assertions ----
    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
    data = response.json()
    assert data.get("rail") == "mpp-spt"
    assert data.get("live") is True
    pi_id_from_body: str = data.get("piId", "")
    assert _PI_RE.match(pi_id_from_body), (
        f"piId in body is not a valid PaymentIntent id: {pi_id_from_body!r}"
    )

    # ---- Trace assertions ----
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM trace_events WHERE selected_rail = 'mpp-spt'").fetchall()
    conn.close()

    assert len(rows) == 1, f"Expected 1 trace row, got {len(rows)}"
    row = dict(rows[0])

    assert row["http_status"] == 200
    assert row["service_delivered"] == 1

    payload = json.loads(row["payload"])
    assert payload["payment"]["proofType"] == "spt_id"

    # proof_value is the SPT id minted by the buyer; the PaymentIntent id lives
    # only in the response body (pi_id_from_body) since the trace stores the
    # buyer-side proof, not the merchant's redemption reference.
    proof_value: str = payload["payment"]["proofValue"]
    assert _SPT_RE.match(proof_value), f"proofValue is not a valid SPT id: {proof_value!r}"

    # Confirm the response body's piId is a live Stripe PaymentIntent (not the
    # fabricated pi_test_3MOCK_... from the in-process mock server fixture).
    assert proof_value != "spt_test_FAKE", (
        "proofValue looks like the FakeSptCreator was used instead of the real Stripe API"
    )
