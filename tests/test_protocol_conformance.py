"""Verify that all RailAdapter implementations satisfy the Protocol.

This replaces the import-time ``assert isinstance(Adapter([]), RailAdapter)``
checks that were previously at the bottom of each adapter module.  Moving the
check here keeps side effects out of production import paths.
"""

from __future__ import annotations

from routewiler.rails.base import RailAdapter
from routewiler.rails.l402 import L402Adapter
from routewiler.rails.mpp_spt import MppSptAdapter
from routewiler.rails.mpp_tempo import MppTempoAdapter
from routewiler.rails.x402 import X402Adapter


def test_x402_adapter_satisfies_rail_adapter_protocol() -> None:
    assert isinstance(X402Adapter([]), RailAdapter)


def test_l402_adapter_satisfies_rail_adapter_protocol() -> None:
    assert isinstance(L402Adapter([]), RailAdapter)


def test_mpp_tempo_adapter_satisfies_rail_adapter_protocol() -> None:
    assert isinstance(MppTempoAdapter([]), RailAdapter)


def test_mpp_spt_adapter_satisfies_rail_adapter_protocol() -> None:
    assert isinstance(MppSptAdapter([]), RailAdapter)
