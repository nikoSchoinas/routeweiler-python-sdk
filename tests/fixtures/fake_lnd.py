"""Fake LND client for tests — deterministic preimage, no gRPC required."""

from __future__ import annotations

from tests.fixtures.l402_mock_server import MOCK_PREIMAGE

from routeweiler.errors import InvoicePaymentError


class FakeLndClient:
    """Minimal LightningNodeClient that returns a deterministic preimage."""

    def __init__(self, preimage: bytes = MOCK_PREIMAGE, *, should_fail: bool = False) -> None:
        self._preimage = preimage
        self._fail = should_fail

    async def pay_invoice(self, bolt11: str, *, max_fee_msat: int) -> bytes:
        if self._fail:
            raise InvoicePaymentError("Fake payment failure: no_route")
        return self._preimage

    async def get_node_pubkey(self) -> str:
        return "03" + "ab" * 32
