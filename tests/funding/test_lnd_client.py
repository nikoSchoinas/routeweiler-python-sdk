"""Unit tests for LndClient — mocked against lndgrpc.LNDClient."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from routeweiler.errors import InvoicePaymentError
from routeweiler.funding.lightning import _LND_STATUS_FAILED, _LND_STATUS_SUCCEEDED, LndClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DUMMY_BOLT11 = "lnbcrt50000n1" + "a" * 200
_MOCK_PREIMAGE_HEX = "0102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f20"
_MOCK_PREIMAGE_BYTES = bytes.fromhex(_MOCK_PREIMAGE_HEX)


def _make_success_payment(preimage: bytes = _MOCK_PREIMAGE_BYTES) -> MagicMock:
    payment = MagicMock()
    payment.status = _LND_STATUS_SUCCEEDED
    payment.payment_preimage = preimage.hex()
    return payment


def _make_failed_payment(reason: str = "ROUTE_NOT_FOUND") -> MagicMock:
    payment = MagicMock()
    payment.status = _LND_STATUS_FAILED
    payment.failure_reason = reason
    return payment


def _make_pending_payment() -> MagicMock:
    payment = MagicMock()
    payment.status = 1  # IN_FLIGHT — not a terminal status
    return payment


def _make_lnd_client(
    *,
    grpc_host: str = "127.0.0.1",
    grpc_port: int = 10009,
    macaroon_path: str | None = None,
    macaroon_hex: str | None = None,
    tls_cert_path: str | None = None,
    tls_cert_pem: str | None = None,
) -> LndClient:
    return LndClient(
        grpc_host=grpc_host,
        grpc_port=grpc_port,
        macaroon_path=macaroon_path,
        macaroon_hex=macaroon_hex,
        tls_cert_path=tls_cert_path,
        tls_cert_pem=tls_cert_pem,
    )


# ---------------------------------------------------------------------------
# _make_client — constructor arg forwarding
# ---------------------------------------------------------------------------


def test_make_client_uses_macaroon_path() -> None:
    client = _make_lnd_client(macaroon_path="/home/user/.lnd/admin.macaroon")
    with patch("lndgrpc.LNDClient") as mock_cls:
        mock_cls.return_value = MagicMock()
        client._make_client()
        call_kwargs = mock_cls.call_args.kwargs
        assert call_kwargs.get("macaroon_filepath") == "/home/user/.lnd/admin.macaroon"
        assert "macaroon" not in call_kwargs


def test_make_client_uses_macaroon_hex() -> None:
    hex_mac = "0102030405"
    client = _make_lnd_client(macaroon_hex=hex_mac)
    with patch("lndgrpc.LNDClient") as mock_cls:
        mock_cls.return_value = MagicMock()
        client._make_client()
        call_kwargs = mock_cls.call_args.kwargs
        assert call_kwargs.get("macaroon") == bytes.fromhex(hex_mac)
        assert "macaroon_filepath" not in call_kwargs


def test_make_client_uses_tls_cert_path() -> None:
    client = _make_lnd_client(tls_cert_path="/home/user/.lnd/tls.cert")
    with patch("lndgrpc.LNDClient") as mock_cls:
        mock_cls.return_value = MagicMock()
        client._make_client()
        call_kwargs = mock_cls.call_args.kwargs
        assert call_kwargs.get("cert_filepath") == "/home/user/.lnd/tls.cert"
        assert "cert" not in call_kwargs


def test_make_client_uses_tls_cert_pem() -> None:
    pem = "-----BEGIN CERTIFICATE-----\nABC\n-----END CERTIFICATE-----"
    client = _make_lnd_client(tls_cert_pem=pem)
    with patch("lndgrpc.LNDClient") as mock_cls:
        mock_cls.return_value = MagicMock()
        client._make_client()
        call_kwargs = mock_cls.call_args.kwargs
        assert call_kwargs.get("cert") == pem.encode()
        assert "cert_filepath" not in call_kwargs


def test_make_client_uses_correct_ip_address() -> None:
    client = _make_lnd_client(grpc_host="node.example.com", grpc_port=10010)
    with patch("lndgrpc.LNDClient") as mock_cls:
        mock_cls.return_value = MagicMock()
        client._make_client()
        call_kwargs = mock_cls.call_args.kwargs
        assert call_kwargs.get("ip_address") == "node.example.com:10010"


# ---------------------------------------------------------------------------
# pay_invoice — success path
# ---------------------------------------------------------------------------


async def test_pay_invoice_returns_preimage_on_success() -> None:
    lnd_client = _make_lnd_client()
    mock_lnd = MagicMock()
    mock_lnd.send_payment_v2.return_value = iter([_make_success_payment()])

    with patch("lndgrpc.LNDClient", return_value=mock_lnd):
        result = await lnd_client.pay_invoice(_DUMMY_BOLT11, max_fee_msat=1000)

    assert result == _MOCK_PREIMAGE_BYTES


async def test_pay_invoice_handles_bytes_preimage() -> None:
    """LND may return the preimage as raw bytes rather than a hex string."""
    lnd_client = _make_lnd_client()
    payment = MagicMock()
    payment.status = _LND_STATUS_SUCCEEDED
    payment.payment_preimage = _MOCK_PREIMAGE_BYTES  # bytes, not hex string

    mock_lnd = MagicMock()
    mock_lnd.send_payment_v2.return_value = iter([payment])

    with patch("lndgrpc.LNDClient", return_value=mock_lnd):
        result = await lnd_client.pay_invoice(_DUMMY_BOLT11, max_fee_msat=500)

    assert result == _MOCK_PREIMAGE_BYTES


async def test_pay_invoice_skips_pending_before_success() -> None:
    """Pending (IN_FLIGHT) events before terminal status are ignored."""
    lnd_client = _make_lnd_client()
    mock_lnd = MagicMock()
    mock_lnd.send_payment_v2.return_value = iter(
        [_make_pending_payment(), _make_pending_payment(), _make_success_payment()]
    )

    with patch("lndgrpc.LNDClient", return_value=mock_lnd):
        result = await lnd_client.pay_invoice(_DUMMY_BOLT11, max_fee_msat=1000)

    assert result == _MOCK_PREIMAGE_BYTES


# ---------------------------------------------------------------------------
# pay_invoice — forwarding max_fee_msat
# ---------------------------------------------------------------------------


async def test_pay_invoice_forwards_fee_limit() -> None:
    lnd_client = _make_lnd_client()
    mock_lnd = MagicMock()
    mock_lnd.send_payment_v2.return_value = iter([_make_success_payment()])

    with patch("lndgrpc.LNDClient", return_value=mock_lnd):
        await lnd_client.pay_invoice(_DUMMY_BOLT11, max_fee_msat=5000)

    call_kwargs = mock_lnd.send_payment_v2.call_args.kwargs
    assert call_kwargs.get("fee_limit_msat") == 5000


# ---------------------------------------------------------------------------
# pay_invoice — failure path
# ---------------------------------------------------------------------------


async def test_pay_invoice_raises_on_failed_status() -> None:
    lnd_client = _make_lnd_client()
    mock_lnd = MagicMock()
    mock_lnd.send_payment_v2.return_value = iter([_make_failed_payment("TIMEOUT")])

    with patch("lndgrpc.LNDClient", return_value=mock_lnd):
        with pytest.raises(InvoicePaymentError, match="TIMEOUT"):
            await lnd_client.pay_invoice(_DUMMY_BOLT11, max_fee_msat=1000)


async def test_pay_invoice_raises_when_stream_ends_without_terminal() -> None:
    """Stream ends without SUCCEEDED or FAILED → InvoicePaymentError."""
    lnd_client = _make_lnd_client()
    mock_lnd = MagicMock()
    mock_lnd.send_payment_v2.return_value = iter([_make_pending_payment()])

    with patch("lndgrpc.LNDClient", return_value=mock_lnd):
        with pytest.raises(InvoicePaymentError, match="terminal status"):
            await lnd_client.pay_invoice(_DUMMY_BOLT11, max_fee_msat=1000)


# ---------------------------------------------------------------------------
# get_node_pubkey
# ---------------------------------------------------------------------------


async def test_get_node_pubkey_returns_identity_pubkey() -> None:
    lnd_client = _make_lnd_client()
    expected_pubkey = "03" + "ab" * 32
    mock_info = MagicMock()
    mock_info.identity_pubkey = expected_pubkey
    mock_lnd = MagicMock()
    mock_lnd.get_info.return_value = mock_info

    with patch("lndgrpc.LNDClient", return_value=mock_lnd):
        pubkey = await lnd_client.get_node_pubkey()

    assert pubkey == expected_pubkey
