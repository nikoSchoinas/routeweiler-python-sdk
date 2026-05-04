"""FMV (Fair Market Value) conversion for budget cap enforcement.

Cap enforcement converts rail-native amounts to the envelope's declared currency
using rates cached at envelope creation and refreshed lazily if the snapshot is
older than 24 hours.

Conversion rules (§10.3):
  1. Stablecoin peg matching (USDC→USD, EURC→EUR): 1:1, ``fmv_quality="stablecoin_peg"``.
  2. Stablecoin non-matching (USDC→EUR): peg to USD then ECB rate, ``fmv_quality="fx_leg"``.
  3. Sats/BTC → envelope currency: CoinGecko simple-price, ``fmv_quality="coingecko_simple"``.
  4. No cached rate: raises ``FmvUnavailableError`` (never silently bypasses the cap).

A 5% buffer (``FMV_BUFFER``) is applied on cross-currency conversions so intra-day
price moves don't silently breach the cap (§8.4).

The ECB rate integration in this module is a stub returning hardcoded rates.
Real ECB XML feed integration ships in a later week.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal
from typing import TYPE_CHECKING

from routewiler._constants import FMV_BUFFER
from routewiler.assets import ASSETS_BY_ADDRESS
from routewiler.errors import FmvUnavailableError

if TYPE_CHECKING:
    from routewiler.trace.schema import FmvQuality

# ---------------------------------------------------------------------------
# Derived stablecoin peg tables — computed from the central asset registry.
# Adding a new stablecoin only requires an entry in assets.py.
# ---------------------------------------------------------------------------

STABLECOIN_PEG: dict[str, str] = {
    addr: meta.peg_currency
    for addr, meta in ASSETS_BY_ADDRESS.items()
    if meta.peg_currency is not None
}

STABLECOIN_DECIMALS = 6  # USDC and EURC both use 6 decimal places (held constant for now)

# Minor units per major unit for each envelope currency.
MINOR_PER_MAJOR: dict[str, int] = {"usd": 100, "eur": 100, "gbp": 100, "jpy": 1}

# ---------------------------------------------------------------------------
# ECB rate stub — hardcoded reference rates (USD as pivot).
# Real ECB XML feed integration ships in a later week.
# ---------------------------------------------------------------------------

_ECB_STUB: dict[tuple[str, str], Decimal] = {
    ("usd", "eur"): Decimal("0.92"),
    ("eur", "usd"): Decimal("1.087"),
    ("usd", "gbp"): Decimal("0.79"),
    ("gbp", "usd"): Decimal("1.266"),
    ("usd", "jpy"): Decimal("153.5"),
    ("jpy", "usd"): Decimal("0.00652"),
    ("eur", "gbp"): Decimal("0.858"),
    ("gbp", "eur"): Decimal("1.166"),
}


def ecb_rate_stub(src: str, dst: str) -> Decimal | None:
    """Return a hardcoded ECB reference rate for src→dst, or None if unknown."""
    if src == dst:
        return Decimal("1")
    return _ECB_STUB.get((src.lower(), dst.lower()))


# ---------------------------------------------------------------------------
# Core conversion — used by BudgetStore.draw() for cap enforcement.
# ---------------------------------------------------------------------------


def _erc20_address(caip19: str) -> str | None:
    """Extract the lowercase ERC-20 address from a CAIP-19 string, or None."""
    if "/erc20:" in caip19:
        return caip19.rsplit("/erc20:", maxsplit=1)[-1].lower()
    return None


def amount_to_envelope_minor_units(
    rail_currency: str,
    amount_native: int,
    envelope_currency: str,
    *,
    snapshot_rates: dict[str, Decimal] | None = None,
) -> tuple[int, FmvQuality]:
    """Convert rail-native base units to envelope minor units (ceiling rounding).

    Returns ``(minor_units, fmv_quality)``.

    For stablecoin-peg matching pairs this is purely arithmetic (no snapshot
    needed).  For cross-currency pairs the ``snapshot_rates`` dict (keyed as
    ``"<from>-><to>"``) is consulted; if no rate is available,
    ``FmvUnavailableError`` is raised.

    The 5% ``FMV_BUFFER`` (§8.4) is applied to all non-peg conversions.
    """
    env_cur = envelope_currency.lower()
    address = _erc20_address(rail_currency)

    if address and address in STABLECOIN_PEG:
        peg = STABLECOIN_PEG[address]
        minor_per_major = MINOR_PER_MAJOR.get(env_cur, 100)
        divisor = 10**STABLECOIN_DECIMALS

        if peg == env_cur:
            # 1:1 stablecoin peg — exact ceiling arithmetic, no buffer.
            result = (amount_native * minor_per_major + divisor - 1) // divisor
            return result, "stablecoin_peg"

        # Stablecoin in a non-matching envelope currency (e.g. USDC in EUR envelope).
        # Convert peg currency → envelope currency via ECB rate.
        rate = _resolve_rate(peg, env_cur, snapshot_rates)
        amount_major = Decimal(amount_native) / Decimal(divisor)
        result = _apply_rate_with_buffer(amount_major, rate, minor_per_major)
        return result, "fx_leg"

    # Sats / native BTC: ``"btc-lightning"`` or similar non-ERC-20 currency string.
    # Requires a snapshot rate keyed ``"sats-><env_cur>"``.
    if "sats" in rail_currency.lower() or "btc" in rail_currency.lower():
        _minor_per_major = MINOR_PER_MAJOR.get(env_cur, 100)
        rate = _resolve_rate("sats", env_cur, snapshot_rates)
        amount_major = Decimal(amount_native)
        result = _apply_rate_with_buffer(amount_major, rate, _minor_per_major)
        return result, "coingecko_simple"

    raise FmvUnavailableError(
        f"No FMV conversion available for asset '{rail_currency}' → '{envelope_currency}'. "
        "Ensure a snapshot has been captured for this envelope."
    )


def _resolve_rate(src: str, dst: str, snapshot_rates: dict[str, Decimal] | None) -> Decimal:
    """Return the conversion rate src→dst from snapshot_rates or ECB stub.

    Raises ``FmvUnavailableError`` if neither source has a rate.
    """
    key = f"{src}->{dst}"
    if snapshot_rates:
        rate = snapshot_rates.get(key)
        if rate is not None:
            return rate

    # Fall back to ECB stub for fiat-to-fiat pairs.
    rate = ecb_rate_stub(src, dst)
    if rate is not None:
        return rate

    raise FmvUnavailableError(
        f"No cached FMV rate for '{src}'→'{dst}'. "
        "Envelope FMV snapshot may be stale or CoinGecko was unavailable at creation."
    )


def _apply_rate_with_buffer(amount_major: Decimal, rate: Decimal, minor_per_major: int) -> int:
    """Apply rate + 5% buffer and return ceiling minor units."""
    buffered = amount_major * rate * (Decimal("1") + FMV_BUFFER)
    minor = buffered * Decimal(minor_per_major)
    # Ceiling to the next integer minor unit.
    return int(minor.to_integral_value(rounding=ROUND_CEILING))


# ---------------------------------------------------------------------------
# Stablecoin-peg FMV for trace emission (no buffer — post-settlement only).
# Called by trace/emitter.py; never on the payment call path.
# ---------------------------------------------------------------------------


def fmv_for_trace(
    caip19_currency: str,
    amount_native: int,
    envelope_currency: str,
    snapshot_rates: dict[str, Decimal] | None = None,
) -> tuple[float | None, FmvQuality]:
    """Compute FMV for trace emission — never blocks a payment.

    Returns ``(amount_envelope_float, fmv_quality)``.  On any failure returns
    ``(None, "unavailable")`` rather than raising.
    """
    env_cur = envelope_currency.lower()
    address = _erc20_address(caip19_currency)

    if address and address in STABLECOIN_PEG:
        peg = STABLECOIN_PEG[address]
        divisor = 10**STABLECOIN_DECIMALS
        if peg == env_cur:
            return amount_native / divisor, "stablecoin_peg"
        # Cross-currency stablecoin — try snapshot rate.
        if snapshot_rates:
            key = f"{peg}->{env_cur}"
            rate = snapshot_rates.get(key)
            if rate is not None:
                amount_major = amount_native / divisor
                return float(Decimal(str(amount_major)) * rate), "fx_leg"

    # Sats / native BTC (e.g. "btc-lightning") — informational only, no 5% buffer.
    if "sats" in caip19_currency.lower() or "btc" in caip19_currency.lower():
        if snapshot_rates:
            key = f"sats->{env_cur}"
            rate = snapshot_rates.get(key)
            if rate is not None:
                return float(Decimal(str(amount_native)) * rate), "coingecko_simple"

    return None, "unavailable"


# ---------------------------------------------------------------------------
# Snapshot capture — called at envelope creation to seed envelope_fmv_snapshots.
# Real CoinGecko integration ships in a later week; for now only fiat/stablecoin
# pairs are populated so cross-currency stablecoin cap enforcement works.
# ---------------------------------------------------------------------------


def capture_fmv_snapshot(
    cap_currency: str,
    *,
    sats_rates: dict[str, Decimal] | None = None,
) -> tuple[dict[str, Decimal], dict[str, str]]:
    """Return the initial FMV snapshot rates and quality flags for a new envelope.

    Populates all ECB-stub fiat-to-fiat pairs that include ``cap_currency`` as the
    destination, plus the identity pair ``<cur>-><cur> = 1``.

    ``sats_rates`` is an optional dict of pre-fetched per-satoshi rates keyed as
    ``"sats-><currency>"`` (e.g. ``{"sats->usd": Decimal("0.00000065")}``).  When
    provided they are merged into the snapshot with ``fmv_quality="coingecko_simple"``.
    When omitted the snapshot has no sats entries; cap enforcement on BTC/L402 rails
    will raise ``FmvUnavailableError`` until a snapshot with sats rates is available.

    Returns ``(rates_dict, quality_dict)`` keyed as ``"<from>-><to>"``.
    """
    env_cur = cap_currency.lower()
    rates: dict[str, Decimal] = {}
    quality: dict[str, str] = {}

    # Identity rate (always 1:1).
    rates[f"{env_cur}->{env_cur}"] = Decimal("1")
    quality[f"{env_cur}->{env_cur}"] = "stablecoin_peg"

    # Populate all known fiat-to-fiat cross rates whose destination is env_cur.
    for (src, dst), rate in _ECB_STUB.items():
        if dst == env_cur:
            rates[f"{src}->{dst}"] = rate
            quality[f"{src}->{dst}"] = "fx_leg"

    if sats_rates:
        for key, rate in sats_rates.items():
            rates[key] = rate
            quality[key] = "coingecko_simple"

    return rates, quality
