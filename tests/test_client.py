"""End-to-end tests for the Routewiler async client using respx mocks."""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from routewiler import Funding, Routewiler
from routewiler.errors import RailNotSupportedError


def _encode_challenge(data: dict) -> str:  # type: ignore[type-arg]
    return base64.b64encode(json.dumps(data).encode()).decode()


_CHALLENGE = {
    "accepts": [
        {
            "scheme": "exact",
            "network": "base",
            "maxAmountRequired": "1000",
            "resource": "https://api.example.com/data",
            "description": "Test endpoint",
            "mimeType": "application/json",
            "payTo": "0x1234567890123456789012345678901234567890",
            "maxTimeoutSeconds": 60,
            "asset": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
            "extra": {"nonce": "0xabc", "validBefore": 9999999999, "validAfter": 0},
        }
    ]
}
_PAYMENT_REQUIRED_HEADER = _encode_challenge(_CHALLENGE)
_SIGNED_PAYLOAD = base64.b64encode(b'{"signature":"0xtest"}').decode()


@pytest.fixture
def routewiler_client(test_account) -> Routewiler:  # type: ignore[no-untyped-def]
    return Routewiler(funding=[Funding.base_usdc(wallet=test_account)])


@respx.mock
async def test_happy_path_402_then_200(routewiler_client: Routewiler) -> None:
    """A 402 response triggers a signed retry that returns 200."""
    url = "https://api.example.com/data"

    with patch(
        "routewiler.rails.x402.x402Client",
    ) as mock_cls:
        mock_instance = AsyncMock()
        mock_instance.create_payment_payload.return_value = {"signature": "0xtest"}
        mock_cls.return_value = mock_instance

        # Re-create client so it picks up the patched x402Client

        client = Routewiler(
            funding=[Funding.base_usdc(wallet=routewiler_client._funding[0].wallet)]
        )

        # First call → 402; second call → 200
        route = respx.get(url)
        route.side_effect = [
            httpx.Response(
                status_code=402,
                headers={"PAYMENT-REQUIRED": _PAYMENT_REQUIRED_HEADER},
                content=b"payment required",
            ),
            httpx.Response(status_code=200, json={"result": "ok"}),
        ]

        resp = await client.get(url)

    assert resp.status_code == 200
    assert resp.json() == {"result": "ok"}
    # Second request must carry PAYMENT-SIGNATURE
    assert route.call_count == 2
    last_request = route.calls[-1].request
    assert "PAYMENT-SIGNATURE" in last_request.headers


@respx.mock
async def test_200_passthrough(routewiler_client: Routewiler) -> None:
    """Non-402 responses pass through without payment."""
    respx.get("https://api.example.com/free").mock(
        return_value=httpx.Response(200, json={"free": True})
    )
    resp = await routewiler_client.get("https://api.example.com/free")
    assert resp.status_code == 200
    assert resp.json()["free"] is True


@respx.mock
async def test_unsupported_rail_raises(routewiler_client: Routewiler) -> None:
    """A 402 with no PAYMENT-REQUIRED header raises RailNotSupportedError."""
    respx.get("https://api.example.com/l402").mock(
        return_value=httpx.Response(
            402, headers={"WWW-Authenticate": 'L402 macaroon="abc", invoice="lnbc..."'}
        )
    )
    with pytest.raises(RailNotSupportedError):
        await routewiler_client.get("https://api.example.com/l402")


async def test_context_manager(test_account) -> None:  # type: ignore[no-untyped-def]
    async with Routewiler(funding=[Funding.base_usdc(wallet=test_account)]) as client:
        assert client._http is not None
