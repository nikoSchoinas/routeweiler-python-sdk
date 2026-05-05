"""Rail adapters — one per payment protocol."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from routewiler.funding.evm import EvmFundingSource
from routewiler.funding.lightning import LightningFundingSource
from routewiler.funding.tempo import TempoFundingSource
from routewiler.rails.base import RailAdapter
from routewiler.rails.l402 import L402Adapter
from routewiler.rails.mpp_tempo import MppTempoAdapter
from routewiler.rails.x402 import X402Adapter

if TYPE_CHECKING:
    from routewiler.funding import FundingSource

__all__ = ["ADAPTER_REGISTRY", "L402Adapter", "MppTempoAdapter", "RailAdapter", "X402Adapter"]


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


# Each entry is a factory: (list[FundingSource]) -> RailAdapter | None.
# Month 4 W14: append _mpp_spt_factory for Stripe SPT (fiat fallback).
ADAPTER_REGISTRY: list[Callable[[list[FundingSource]], RailAdapter | None]] = [
    _x402_factory,
    _l402_factory,
    _mpp_tempo_factory,
]
