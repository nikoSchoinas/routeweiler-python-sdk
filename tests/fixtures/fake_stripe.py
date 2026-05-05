"""FakeSptCreator — deterministic synthetic SPT creator for tests.

Returned SPT id is always ``FAKE_SPT_ID``.  Call-count tracking and
on-demand failure injection let tests exercise error paths without
hitting the Stripe API.
"""

from __future__ import annotations

from typing import Any

FAKE_SPT_ID = "spt_test_FAKE00000000000000000000"


class FakeSptCreator:
    """SptCreator that returns a deterministic SPT id without calling Stripe.

    Args:
        fail_with: When set, ``create_spt`` raises this exception instead of
                   returning ``FAKE_SPT_ID``.  Useful for testing
                   ``SptCreationError`` paths.
    """

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self._fail_with = fail_with
        self.call_count = 0
        self.last_kwargs: dict[str, Any] = {}

    async def create_spt(
        self,
        *,
        usage_limits: dict[str, Any],
        seller_details: dict[str, Any],
        payment_method: str,
        customer: str,
    ) -> str:
        self.call_count += 1
        self.last_kwargs = {
            "usage_limits": usage_limits,
            "seller_details": seller_details,
            "payment_method": payment_method,
            "customer": customer,
        }
        if self._fail_with is not None:
            raise self._fail_with
        return FAKE_SPT_ID
