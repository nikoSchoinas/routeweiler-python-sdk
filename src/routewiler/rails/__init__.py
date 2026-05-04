"""Rail adapters — one per payment protocol."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from routewiler.funding.evm import EvmFundingSource
from routewiler.rails.base import RailAdapter
from routewiler.rails.x402 import X402Adapter

if TYPE_CHECKING:
    from routewiler.funding import FundingSource

__all__ = ["ADAPTER_REGISTRY", "RailAdapter", "X402Adapter"]


def _x402_factory(funding: list[FundingSource]) -> RailAdapter | None:
    """Return an X402Adapter if any EVM funding sources are present."""
    evm = [f for f in funding if isinstance(f, EvmFundingSource)]
    if not evm:
        return None
    return X402Adapter(evm)


# Each entry is a factory: (list[FundingSource]) -> RailAdapter | None.
# Month 3: append _l402_factory after LightningFundingSource lands.
# Month 4: append _mpp_factory after Stripe/Tempo funding lands.
ADAPTER_REGISTRY: list[Callable[[list[FundingSource]], RailAdapter | None]] = [
    _x402_factory,
]
