"""Rail adapters — one per payment protocol."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from routeweiler.funding.evm import EvmFundingSource
from routeweiler.funding.lightning import LightningFundingSource
from routeweiler.funding.stripe import StripeFundingSource
from routeweiler.funding.tempo import TempoFundingSource
from routeweiler.rails.base import RailAdapter
from routeweiler.rails.l402 import L402Adapter
from routeweiler.rails.mpp_spt import MppSptAdapter
from routeweiler.rails.mpp_tempo import MppTempoAdapter
from routeweiler.rails.x402 import X402Adapter

if TYPE_CHECKING:
    from routeweiler.funding import FundingSource

__all__ = [
    "ADAPTER_REGISTRY",
    "L402Adapter",
    "MppSptAdapter",
    "MppTempoAdapter",
    "RailAdapter",
    "X402Adapter",
]


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


def _mpp_tempo_factory(funding: list[FundingSource]) -> RailAdapter | None:
    """Return an MppTempoAdapter if any Tempo funding sources are present."""
    tempo = [f for f in funding if isinstance(f, TempoFundingSource)]
    if not tempo:
        return None
    return MppTempoAdapter(tempo)


def _mpp_spt_factory(funding: list[FundingSource]) -> RailAdapter | None:
    """Return an MppSptAdapter if any Stripe funding sources are present."""
    stripe = [f for f in funding if isinstance(f, StripeFundingSource)]
    if not stripe:
        return None
    return MppSptAdapter(stripe)


# Each entry is a factory: (list[FundingSource]) -> RailAdapter | None.
ADAPTER_REGISTRY: list[Callable[[list[FundingSource]], RailAdapter | None]] = [
    _x402_factory,
    _l402_factory,
    _mpp_tempo_factory,
    _mpp_spt_factory,
]
