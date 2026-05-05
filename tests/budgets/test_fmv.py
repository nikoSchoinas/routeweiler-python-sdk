"""Unit tests for budgets/fmv.py — FMV conversion and stablecoin peg tables."""

from __future__ import annotations

from decimal import Decimal

import pytest

from routewiler.budgets.fmv import (
    amount_to_envelope_minor_units,
    capture_fmv_snapshot,
    ecb_rate_stub,
)
from routewiler.errors import FmvUnavailableError

_USDC_BASE = "eip155:8453/erc20:0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
_USDC_SEPOLIA = "eip155:84532/erc20:0x036cbd53842c5426634e7929541ec2318f3dcf7e"
_EURC_BASE = "eip155:8453/erc20:0x60a3e35cc302bfa44cb288bc5a4f316fdb1adb42"

# ---------------------------------------------------------------------------
# Stablecoin peg — matching envelope currency
# ---------------------------------------------------------------------------


def test_usdc_to_usd_exact_cent() -> None:
    result, quality = amount_to_envelope_minor_units(_USDC_BASE, 10_000, "usd")
    assert result == 1
    assert quality == "stablecoin_peg"


def test_usdc_sepolia_to_usd_sub_cent_rounds_up() -> None:
    result, quality = amount_to_envelope_minor_units(_USDC_SEPOLIA, 1_000, "usd")
    assert result == 1  # ceiling
    assert quality == "stablecoin_peg"


def test_usdc_to_usd_one_dollar() -> None:
    result, _ = amount_to_envelope_minor_units(_USDC_BASE, 1_000_000, "usd")
    assert result == 100


def test_eurc_to_eur() -> None:
    result, quality = amount_to_envelope_minor_units(_EURC_BASE, 10_000, "eur")
    assert result == 1
    assert quality == "stablecoin_peg"


# ---------------------------------------------------------------------------
# Cross-currency stablecoin (fx_leg)
# ---------------------------------------------------------------------------


def test_usdc_to_eur_with_snapshot_rate() -> None:
    rates = {"usd->eur": Decimal("0.92")}
    # 1 USDC -> 0.92 EUR * 1.05 buffer = 0.966 EUR -> 97 EUR cents (ceiling)
    result, quality = amount_to_envelope_minor_units(
        _USDC_BASE, 1_000_000, "eur", snapshot_rates=rates
    )
    assert result == 97
    assert quality == "fx_leg"


def test_usdc_to_eur_falls_back_to_ecb_stub_when_no_snapshot() -> None:
    # No snapshot_rates → falls back to hardcoded ECB stub USD→EUR (0.92)
    result, quality = amount_to_envelope_minor_units(_USDC_BASE, 1_000_000, "eur")
    assert result == 97
    assert quality == "fx_leg"


# ---------------------------------------------------------------------------
# Sats / BTC (coingecko_simple)
# ---------------------------------------------------------------------------


def test_sats_to_usd_with_snapshot() -> None:
    # 1 sat * rate * 1.05 buffer * 100 cents
    rates = {"sats->usd": Decimal("0.00065")}
    result, quality = amount_to_envelope_minor_units(
        "btc-lightning", 100_000, "usd", snapshot_rates=rates
    )
    # 100000 sats * 0.00065 * 1.05 * 100 = 6825 -> 6825 cents
    assert result == 6825
    assert quality == "coingecko_simple"


def test_sats_without_snapshot_raises() -> None:
    with pytest.raises(FmvUnavailableError):
        amount_to_envelope_minor_units("btc-lightning", 50_000, "usd")


# ---------------------------------------------------------------------------
# Unknown asset
# ---------------------------------------------------------------------------


def test_unknown_asset_raises() -> None:
    with pytest.raises(FmvUnavailableError):
        amount_to_envelope_minor_units("eip155:1/erc20:0xdeadbeef", 1000, "usd")


# ---------------------------------------------------------------------------
# ECB stub
# ---------------------------------------------------------------------------


def test_ecb_stub_same_currency() -> None:
    assert ecb_rate_stub("usd", "usd") == Decimal("1")


def test_ecb_stub_known_pair() -> None:
    rate = ecb_rate_stub("usd", "eur")
    assert rate is not None
    assert 0 < float(rate) < 2


def test_ecb_stub_unknown_pair() -> None:
    assert ecb_rate_stub("usd", "xyz") is None


# ---------------------------------------------------------------------------
# capture_fmv_snapshot
# ---------------------------------------------------------------------------


def test_capture_fmv_snapshot_contains_identity() -> None:
    rates, quality = capture_fmv_snapshot("usd")
    assert "usd->usd" in rates
    assert rates["usd->usd"] == Decimal("1")
    assert quality["usd->usd"] == "stablecoin_peg"


def test_capture_fmv_snapshot_usd_contains_cross_rates() -> None:
    rates, quality = capture_fmv_snapshot("usd")
    # ECB stub has eur->usd, so it should appear.
    assert "eur->usd" in rates
    assert quality["eur->usd"] == "fx_leg"


def test_capture_fmv_snapshot_only_targets_env_currency() -> None:
    usd_rates, _ = capture_fmv_snapshot("usd")
    # All keys in rates must end with "->usd".
    for key in usd_rates:
        assert key.endswith("->usd"), f"Unexpected key {key!r}"


def test_capture_fmv_snapshot_used_by_amount_conversion() -> None:
    rates, _ = capture_fmv_snapshot("eur")
    # USDC in EUR envelope must work end-to-end via snapshot rates.
    result, quality = amount_to_envelope_minor_units(
        _USDC_BASE, 1_000_000, "eur", snapshot_rates=rates
    )
    assert result == 97  # 1 USDC * 0.92 EUR/USD * 1.05 buffer * 100 = 96.6 → 97 cents
    assert quality == "fx_leg"


# ---------------------------------------------------------------------------
# Fiat: "<iso>-fiat" branch — added for MPP-SPT (Week 14)
# ---------------------------------------------------------------------------


def test_fiat_same_currency_passthrough() -> None:
    """usd-fiat in a USD envelope is 1:1, no conversion."""
    result, quality = amount_to_envelope_minor_units("usd-fiat", 500, "usd")
    assert result == 500
    assert quality == "stablecoin_peg"


def test_fiat_same_currency_eur() -> None:
    result, quality = amount_to_envelope_minor_units("eur-fiat", 100, "eur")
    assert result == 100
    assert quality == "stablecoin_peg"


def test_fiat_cross_currency_usd_to_eur() -> None:
    """USD cents → EUR cents via ECB stub (0.92), with 5% buffer, ceil."""
    # 500 cents * 0.92 * 1.05 = 483.0 → 483 EUR cents
    result, quality = amount_to_envelope_minor_units("usd-fiat", 500, "eur")
    assert result == 483
    assert quality == "fx_leg"


def test_fiat_cross_currency_eur_to_usd() -> None:
    """EUR cents → USD cents via ECB stub (1.087), with 5% buffer, ceil."""
    # 100 cents * 1.087 * 1.05 = 114.135 → 115 USD cents
    result, quality = amount_to_envelope_minor_units("eur-fiat", 100, "usd")
    assert result == 115
    assert quality == "fx_leg"


def test_fiat_cross_currency_uses_snapshot_rate_over_ecb() -> None:
    """Custom snapshot rate takes precedence over ECB stub."""
    rates = {"usd->eur": Decimal("0.90")}
    # 200 * 0.90 * 1.05 = 189 cents
    result, quality = amount_to_envelope_minor_units("usd-fiat", 200, "eur", snapshot_rates=rates)
    assert result == 189
    assert quality == "fx_leg"


def test_fiat_unknown_cross_currency_raises() -> None:
    """Unknown fiat cross-pair (no ECB rate) raises FmvUnavailableError."""
    with pytest.raises(FmvUnavailableError):
        amount_to_envelope_minor_units("usd-fiat", 100, "xyz")
