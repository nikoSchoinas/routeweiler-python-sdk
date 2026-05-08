"""FMV (Fair Market Value) conversion for budget cap enforcement.

Cap enforcement converts rail-native amounts to the envelope's declared currency
using rates cached at envelope creation and refreshed lazily if the snapshot is
older than 24 hours.

Conversion rules:
  1. Stablecoin peg matching (USDC→USD, EURC→EUR): 1:1, ``fmv_quality="stablecoin_peg"``.
  2. Stablecoin non-matching (USDC→EUR): peg to USD then ECB rate, ``fmv_quality="fx_leg"``.
  3. Sats/BTC → envelope currency: CoinGecko simple-price, ``fmv_quality="coingecko_simple"``.
  4. No cached rate: raises ``FmvUnavailableError`` (never silently bypasses the cap).

A 5% buffer (``FMV_BUFFER``) is applied on cross-currency conversions so intra-day
price moves don't silently breach the cap.

Cross-fiat rates (EUR↔USD etc.) are pre-fetched at envelope creation via
``LiveEcbProvider`` (``budgets/ecb_provider.py``) and stored in the FMV snapshot.
``_ECB_OFFLINE_FALLBACK`` serves as a last-resort dict if live rates are unavailable;
a warning is emitted whenever it is consulted at draw time.
"""

from __future__ import annotations

import logging
from decimal import ROUND_CEILING, Decimal
from typing import TYPE_CHECKING

from routeweiler._constants import FMV_BUFFER
from routeweiler.assets import ASSETS_BY_ADDRESS
from routeweiler.budgets.schema import EnvelopeCurrency
from routeweiler.errors import FmvUnavailableError

if TYPE_CHECKING:
    from routeweiler.trace.schema import FmvQuality

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Derived stablecoin peg tables — computed from the central asset registry.
# Adding a new stablecoin only requires an entry in assets.py.
# ---------------------------------------------------------------------------

STABLECOIN_PEG: dict[str, str] = {
    addr: meta.peg_currency
    for addr, meta in ASSETS_BY_ADDRESS.items()
    if meta.peg_currency is not None
}

# Decimal places per stablecoin come from the asset registry, not a constant.
# Do not add a global STABLECOIN_DECIMALS — use ASSETS_BY_ADDRESS[address].decimals.

# Minor units per major unit for each envelope currency.
MINOR_PER_MAJOR: dict[str, int] = {"usd": 100, "eur": 100, "gbp": 100, "jpy": 1}

# ---------------------------------------------------------------------------
# ECB offline fallback — hardcoded reference rates (USD as pivot).
# Used when the live ECB provider is unavailable or not configured.
# A warning is emitted at draw time whenever this dict is consulted.
# ---------------------------------------------------------------------------

_ECB_OFFLINE_FALLBACK: dict[tuple[str, str], Decimal] = {
    ("usd", "eur"): Decimal("0.92"),
    ("eur", "usd"): Decimal("1.087"),
    ("usd", "gbp"): Decimal("0.79"),
    ("gbp", "usd"): Decimal("1.266"),
    ("usd", "jpy"): Decimal("153.5"),
    ("jpy", "usd"): Decimal("0.00652"),
    ("eur", "gbp"): Decimal("0.858"),
    ("gbp", "eur"): Decimal("1.166"),
}


def ecb_rate(src: str, dst: str) -> Decimal | None:
    """Return a hardcoded offline ECB reference rate for src→dst, or None if unknown."""
    if src == dst:
        return Decimal("1")
    return _ECB_OFFLINE_FALLBACK.get((src.lower(), dst.lower()))


# ---------------------------------------------------------------------------
# Core conversion — used by BudgetStore.draw() for cap enforcement.
# ---------------------------------------------------------------------------


def _erc20_address(caip19: str) -> str | None:
    """Extract the lowercase ERC-20 address from a CAIP-19 string, or None."""
    if "/erc20:" in caip19:
        return caip19.rsplit("/erc20:", maxsplit=1)[-1].lower()
    return None


def _tip20_address(currency: str) -> str | None:
    """Extract the lowercase contract address from a TIP-20 currency string, or None.

    Tempo tokens use the format ``"<address>-tip20"`` (e.g. ``"0x20c0...-tip20"``).
    """
    if currency.endswith("-tip20"):
        return currency[: -len("-tip20")].lower()
    return None


def amount_to_envelope_minor_units(
    rail_currency: str,
    amount_native: int,
    envelope_currency: EnvelopeCurrency,
    *,
    snapshot_rates: dict[str, Decimal] | None = None,
) -> tuple[int, FmvQuality]:
    """Convert rail-native base units to envelope minor units (ceiling rounding).

    Returns ``(minor_units, fmv_quality)``.

    For stablecoin-peg matching pairs this is purely arithmetic (no snapshot
    needed).  For cross-currency pairs the ``snapshot_rates`` dict (keyed as
    ``"<from>-><to>"``) is consulted; if no rate is available,
    ``FmvUnavailableError`` is raised.

    The 5% ``FMV_BUFFER`` is applied to all non-peg conversions.
    """
    env_cur = envelope_currency.lower()

    # Fiat: "<iso>-fiat" — already in minor units of the named fiat currency.
    # Example: "usd-fiat" with amount=500 means $5.00 (500 cents).
    if rail_currency.endswith("-fiat"):
        src = rail_currency[: -len("-fiat")]
        if src == env_cur:
            return amount_native, "stablecoin_peg"
        rate = _resolve_rate(src, env_cur, snapshot_rates)
        result = _apply_rate_with_buffer(Decimal(amount_native), rate, 1)
        return result, "fx_leg"

    address = _erc20_address(rail_currency) or _tip20_address(rail_currency)

    if address and address in STABLECOIN_PEG:
        peg = STABLECOIN_PEG[address]
        minor_per_major = MINOR_PER_MAJOR[env_cur]
        meta = ASSETS_BY_ADDRESS.get(address)
        decimals = meta.decimals if meta is not None else 6
        divisor = 10**decimals

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
        _minor_per_major = MINOR_PER_MAJOR[env_cur]
        rate = _resolve_rate("sats", env_cur, snapshot_rates)
        amount_major = Decimal(amount_native)
        result = _apply_rate_with_buffer(amount_major, rate, _minor_per_major)
        return result, "coingecko_simple"

    raise FmvUnavailableError(
        f"No FMV conversion available for asset '{rail_currency}' → '{envelope_currency}'. "
        "Ensure a snapshot has been captured for this envelope."
    )


def _resolve_rate(src: str, dst: str, snapshot_rates: dict[str, Decimal] | None) -> Decimal:
    """Return the conversion rate src→dst from snapshot_rates or offline fallback.

    Raises ``FmvUnavailableError`` if neither source has a rate.
    """
    key = f"{src}->{dst}"
    if snapshot_rates:
        rate = snapshot_rates.get(key)
        if rate is not None:
            return rate

    # Fall back to offline reference rates — snapshot is missing this pair.
    # This typically means the envelope was created without a live ECB provider.
    rate = ecb_rate(src, dst)
    if rate is not None:
        _log.warning(
            "Using offline ECB fallback for %s→%s (rate: %s). "
            "Pass ecb_provider to BudgetStore for live rates.",
            src.upper(),
            dst.upper(),
            rate,
        )
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
    envelope_currency: EnvelopeCurrency,
    snapshot_rates: dict[str, Decimal] | None = None,
) -> tuple[float | None, FmvQuality]:
    """Compute FMV for trace emission — never blocks a payment.

    Returns ``(amount_envelope_float, fmv_quality)``.  On any failure returns
    ``(None, "unavailable")`` rather than raising.
    """
    env_cur = envelope_currency.lower()

    # Fiat: "<iso>-fiat" — already in minor units of the named fiat currency.
    if caip19_currency.endswith("-fiat"):
        src = caip19_currency[: -len("-fiat")]
        if src == env_cur:
            return float(amount_native), "stablecoin_peg"
        if snapshot_rates:
            key = f"{src}->{env_cur}"
            rate = snapshot_rates.get(key)
            if rate is None:
                rate = ecb_rate(src, env_cur)
            if rate is not None:
                return float(Decimal(str(amount_native)) * rate), "fx_leg"
        rate = ecb_rate(src, env_cur)
        if rate is not None:
            return float(Decimal(str(amount_native)) * rate), "fx_leg"
        return None, "unavailable"

    address = _erc20_address(caip19_currency) or _tip20_address(caip19_currency)

    if address and address in STABLECOIN_PEG:
        peg = STABLECOIN_PEG[address]
        meta = ASSETS_BY_ADDRESS.get(address)
        decimals = meta.decimals if meta is not None else 6
        divisor = 10**decimals
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
# ---------------------------------------------------------------------------


def capture_fmv_snapshot(
    cap_currency: str,
    *,
    sats_rates: dict[str, Decimal] | None = None,
    cross_rates: dict[str, Decimal] | None = None,
) -> tuple[dict[str, Decimal], dict[str, str]]:
    """Return the initial FMV snapshot rates and quality flags for a new envelope.

    Populates all known fiat-to-fiat pairs that include ``cap_currency`` as the
    destination, plus the identity pair ``<cur>-><cur> = 1``.

    ``cross_rates`` is an optional dict of pre-fetched live rates keyed as
    ``"<src>-><dst>"`` (e.g. ``{"usd->eur": Decimal("0.918")}``).  When provided
    these override the offline fallback for the same pair.  Supply this from
    ``LiveEcbProvider`` to seed envelopes with current market rates.

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
    # Live cross_rates take precedence over the offline fallback dict.
    live = cross_rates or {}
    for (src, dst), offline_rate in _ECB_OFFLINE_FALLBACK.items():
        if dst == env_cur:
            key = f"{src}->{dst}"
            rates[key] = live.get(key, offline_rate)
            quality[key] = "fx_leg"

    if sats_rates:
        for key, rate in sats_rates.items():
            rates[key] = rate
            quality[key] = "coingecko_simple"

    return rates, quality
