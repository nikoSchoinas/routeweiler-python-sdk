"""Rail adapters — one per payment protocol."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from routewiler.funding.evm import EvmFundingSource
from routewiler.funding.lightning import LightningFundingSource
from routewiler.rails.base import RailAdapter
from routewiler.rails.l402 import L402Adapter
from routewiler.rails.x402 import X402Adapter

if TYPE_CHECKING:
    from routewiler.funding import FundingSource

__all__ = ["ADAPTER_REGISTRY", "L402Adapter", "RailAdapter", "X402Adapter"]


def _x402_factory(funding: list[FundingSource]) -> RailAdapter | None:
    """Return an X402Adapter if any EVM funding sources are present."""
    evm = [f for f in funding if isinstance(f, EvmFundingSource)]
    if not evm:
        return None
    return X402Adapter(evm)


def _l402_factory(funding: list[FundingSource]) -> RailAdapter | None:
    """Return an L402Adapter if any Lightning funding sources are present."""
    lightning = [f for f in funding if isinstance(f, LightningFundingSource)]
    if not lightning:
        return None
    return L402Adapter(lightning)


# Each entry is a factory: (list[FundingSource]) -> RailAdapter | None.
# Month 4: append _mpp_factory after Stripe/Tempo funding lands.
ADAPTER_REGISTRY: list[Callable[[list[FundingSource]], RailAdapter | None]] = [
    _x402_factory,
    _l402_factory,
]
