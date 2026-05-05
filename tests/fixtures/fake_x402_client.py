"""FakeX402Client — test double for the x402 SDK client."""

from __future__ import annotations

from typing import Any


class FakeX402Client:
    """Minimal stand-in for ``x402Client`` used in signing tests.

    Attributes:
        return_value: Value returned by ``create_payment_payload``.
        fail_with:    If set, ``create_payment_payload`` raises this exception.
        calls:        Ordered list of ``payment_required`` args received.
    """

    def __init__(
        self,
        *,
        return_value: Any = None,
        fail_with: Exception | None = None,
    ) -> None:
        self.return_value: Any = return_value if return_value is not None else {"x-payment": "sig"}
        self.fail_with = fail_with
        self.calls: list[Any] = []

    async def create_payment_payload(self, payment_required: Any) -> Any:
        self.calls.append(payment_required)
        if self.fail_with is not None:
            raise self.fail_with
        return self.return_value
