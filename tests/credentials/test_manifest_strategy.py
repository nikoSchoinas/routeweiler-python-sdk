"""Unit tests for ManifestRecoveryStrategy."""

from __future__ import annotations

import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import respx

from routeweiler.credentials.manifest_strategy import (
    ManifestRecoveryStrategy,
    _build_authorization_header,
)
from routeweiler.credentials.manifests.loader import ManifestRegistry
from routeweiler.credentials.schema import CredentialRecord, CredentialState, ManualHoldReason

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEST_MANIFEST_YAML = textwrap.dedent(
    """\
    name: test-shop
    domain_matches: "mock"
    flow:
      - challenge_path: "/checkout/*"
        fulfil_path_template: "/orders/{order_id}/fulfil"
        id_extractor: "path:checkout/([^/]+)"
    """
)

_MOCK_MACAROON = "bW9ja21hY2Fyb29u"  # base64("mockmacaroon")
_MOCK_PREIMAGE_HEX = "aabbcc" * 10 + "aabb"  # 32 bytes hex


def _make_credential(
    challenge_url: str = "http://mock/checkout/order_123",
    state: CredentialState = CredentialState.RECOVERING,
) -> CredentialRecord:
    now = datetime.now(UTC)
    return CredentialRecord(
        credential_id="cred-001",
        request_id="req-001",
        rail="l402",
        challenge_url=challenge_url,
        payload={"macaroon": _MOCK_MACAROON, "preimage_hex": _MOCK_PREIMAGE_HEX},
        state=state,
        persisted_at=now,
        last_transition_at=now,
        expires_at=now + timedelta(hours=1),
    )


def _make_registry(tmp_path: Path) -> ManifestRegistry:
    path = tmp_path / "test-shop.yaml"
    path.write_text(_TEST_MANIFEST_YAML, encoding="utf-8")
    return ManifestRegistry.from_paths([path])


# ---------------------------------------------------------------------------
# Success cases
# ---------------------------------------------------------------------------


async def test_strategy_succeeds_on_alternate_url(tmp_path: Path) -> None:
    registry = _make_registry(tmp_path)
    credential = _make_credential()

    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.get("http://mock/orders/order_123/fulfil").mock(
            return_value=httpx.Response(200, json={"status": "fulfilled"})
        )
        async with httpx.AsyncClient() as client:
            strategy = ManifestRecoveryStrategy(registry=registry, client=client)
            outcome = await strategy.recover(credential, last_response=None)

    assert outcome.succeeded is True
    assert outcome.response is not None
    assert outcome.response.status_code == 200
    assert outcome.reason is None


async def test_strategy_passes_correct_authorization_header(tmp_path: Path) -> None:
    registry = _make_registry(tmp_path)
    credential = _make_credential()
    expected_auth = f"L402 {_MOCK_MACAROON}:{_MOCK_PREIMAGE_HEX}"

    captured_headers: list[str] = []

    with respx.mock(assert_all_called=False) as respx_mock:

        def _capture(request: httpx.Request) -> httpx.Response:
            captured_headers.append(request.headers.get("Authorization", ""))
            return httpx.Response(200, json={"ok": True})

        respx_mock.get("http://mock/orders/order_123/fulfil").mock(side_effect=_capture)
        async with httpx.AsyncClient() as client:
            strategy = ManifestRecoveryStrategy(registry=registry, client=client)
            await strategy.recover(credential, last_response=None)

    assert len(captured_headers) == 1
    assert captured_headers[0] == expected_auth


# ---------------------------------------------------------------------------
# Exhaustion cases
# ---------------------------------------------------------------------------


async def test_strategy_exhausts_when_all_alternates_are_4xx(tmp_path: Path) -> None:
    registry = _make_registry(tmp_path)
    credential = _make_credential()

    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.get("http://mock/orders/order_123/fulfil").mock(
            return_value=httpx.Response(404, json={"error": "not_found"})
        )
        async with httpx.AsyncClient() as client:
            strategy = ManifestRecoveryStrategy(registry=registry, client=client)
            outcome = await strategy.recover(credential, last_response=None)

    assert outcome.succeeded is False
    assert outcome.reason == ManualHoldReason.EXHAUSTED


async def test_strategy_no_matching_shape_returns_exhausted(tmp_path: Path) -> None:
    """No manifest matches the credential's URL domain → EXHAUSTED without HTTP calls."""
    path = tmp_path / "other.yaml"
    path.write_text("name: other\ndomain_matches: '*.other.com'\nflow: []\n", encoding="utf-8")
    registry = ManifestRegistry.from_paths([path])
    credential = _make_credential(challenge_url="http://mock/checkout/order_123")

    async with httpx.AsyncClient() as client:
        strategy = ManifestRecoveryStrategy(registry=registry, client=client)
        outcome = await strategy.recover(credential, last_response=None)

    assert outcome.succeeded is False
    assert outcome.reason == ManualHoldReason.EXHAUSTED


async def test_strategy_caps_attempts_at_max_attempts(tmp_path: Path) -> None:
    registry = _make_registry(tmp_path)
    credential = _make_credential()
    call_count = {"n": 0}

    with respx.mock(assert_all_called=False) as respx_mock:

        def _count_and_fail(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(404)

        respx_mock.get("http://mock/orders/order_123/fulfil").mock(side_effect=_count_and_fail)
        async with httpx.AsyncClient() as client:
            strategy = ManifestRecoveryStrategy(registry=registry, client=client, max_attempts=2)
            outcome = await strategy.recover(credential, last_response=None)

    assert outcome.succeeded is False
    # With max_attempts=2 and only one matching step, at most 2 calls are issued
    # (but since there's only one step, exactly 1 call happens before exhaustion).
    assert call_count["n"] <= 2


async def test_strategy_transport_error_is_non_fatal(tmp_path: Path) -> None:
    """A transport error on the recovery request does not raise; returns EXHAUSTED."""
    registry = _make_registry(tmp_path)
    credential = _make_credential()

    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.get("http://mock/orders/order_123/fulfil").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        async with httpx.AsyncClient() as client:
            strategy = ManifestRecoveryStrategy(registry=registry, client=client)
            outcome = await strategy.recover(credential, last_response=None)

    assert outcome.succeeded is False
    assert outcome.reason == ManualHoldReason.EXHAUSTED


# ---------------------------------------------------------------------------
# Missing credential fields
# ---------------------------------------------------------------------------


async def test_strategy_missing_macaroon_returns_exhausted(tmp_path: Path) -> None:
    registry = _make_registry(tmp_path)
    now = datetime.now(UTC)
    credential = CredentialRecord(
        credential_id="cred-bad",
        request_id="req-bad",
        rail="l402",
        challenge_url="http://mock/checkout/order_123",
        payload={"preimage_hex": _MOCK_PREIMAGE_HEX},  # macaroon missing
        state=CredentialState.RECOVERING,
        persisted_at=now,
        last_transition_at=now,
        expires_at=now + timedelta(hours=1),
    )
    async with httpx.AsyncClient() as client:
        strategy = ManifestRecoveryStrategy(registry=registry, client=client)
        outcome = await strategy.recover(credential, last_response=None)

    assert outcome.succeeded is False
    assert outcome.reason == ManualHoldReason.EXHAUSTED


# ---------------------------------------------------------------------------
# _build_authorization_header helper
# ---------------------------------------------------------------------------


def test_build_authorization_header_returns_l402_string() -> None:
    now = datetime.now(UTC)
    credential = CredentialRecord(
        credential_id="c1",
        request_id="r1",
        rail="l402",
        challenge_url="http://mock/checkout/1",
        payload={"macaroon": _MOCK_MACAROON, "preimage_hex": _MOCK_PREIMAGE_HEX},
        state=CredentialState.RECOVERING,
        persisted_at=now,
        last_transition_at=now,
    )
    header = _build_authorization_header(credential)
    assert header == f"L402 {_MOCK_MACAROON}:{_MOCK_PREIMAGE_HEX}"


def test_build_authorization_header_returns_none_for_missing_fields() -> None:
    now = datetime.now(UTC)
    credential = CredentialRecord(
        credential_id="c1",
        request_id="r1",
        rail="l402",
        challenge_url="http://mock/checkout/1",
        payload={},  # neither macaroon nor preimage_hex
        state=CredentialState.RECOVERING,
        persisted_at=now,
        last_transition_at=now,
    )
    assert _build_authorization_header(credential) is None
