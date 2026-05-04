"""Minimal BOLT-11 invoice decoder.

We only decode the five fields L402 needs: amount_msat, payment_hash, expiry,
timestamp, payee pubkey, and description.  Signature verification is deliberately
omitted — that is the server's responsibility, not the client's.  This avoids
the `coincurve` / `libsecp256k1` native-extension dependency entirely.

Spec: https://github.com/lightning/bolts/blob/master/11-payment-encoding.md
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Bech32 constants and helpers (BIP-0173, BOLT-11 uses original bech32)
# ---------------------------------------------------------------------------

_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"

# Amount multipliers: how many millisatoshi per 1 unit with that suffix
_MULT_MSAT: dict[str, int] = {
    "m": 100_000_000,  # milli-BTC  (10^-3 BTC x 10^11 msat/BTC)
    "u": 100_000,  # micro-BTC  (10^-6 BTC x 10^11 msat/BTC)
    "n": 100,  # nano-BTC   (10^-9 BTC x 10^11 msat/BTC)
    # "p" (pico) yields 0.1 msat which is sub-integer; we reject it
}

# Tagged field types (5-bit tag values)
_TAG_PAYMENT_HASH = 1  # p — 32 bytes / 256 bits / 52 data-5 groups
_TAG_DESCRIPTION = 13  # d — UTF-8 string
_TAG_PAYEE_PUBKEY = 19  # n — 33-byte compressed pubkey
_TAG_EXPIRY = 6  # x — big-endian seconds, variable length

# Signature is the last 104 data-5 groups (520 bits / 65 bytes); we strip it.
_SIG_DATA5_LEN = 104


@dataclass(frozen=True)
class DecodedBolt11:
    amount_msat: int | None  # None for zero-amount invoices
    payment_hash_hex: str  # 64 hex chars (32 bytes)
    expiry: int  # seconds; default 3600 per spec
    timestamp: int  # Unix seconds
    payee_pubkey_hex: str | None  # 66 hex chars (33 bytes) or None
    description: str | None


class Bolt11DecodeError(ValueError):
    """Raised when a BOLT-11 invoice cannot be decoded."""


def decode(bolt11: str) -> DecodedBolt11:
    """Decode a BOLT-11 payment request into the fields L402 needs.

    Raises:
        Bolt11DecodeError: if the invoice is malformed or a required field
            (payment_hash) is absent.
    """
    s = bolt11.strip().lower()

    # -----------------------------------------------------------------------
    # 1. Split at the last '1' separator (bech32 separator)
    # -----------------------------------------------------------------------
    sep = s.rfind("1")
    if sep < 2:
        raise Bolt11DecodeError("No bech32 separator found")

    hrp = s[:sep]
    data_str = s[sep + 1 :]

    # Minimum: 6 chars checksum + at least some data
    if len(data_str) < 6 + _SIG_DATA5_LEN + 8:  # timestamp (7) + at least one tag
        raise Bolt11DecodeError(f"Data section too short: {len(data_str)} chars")

    # -----------------------------------------------------------------------
    # 2. Decode bech32 data section into 5-bit groups
    # -----------------------------------------------------------------------
    data5: list[int] = []
    for ch in data_str:
        idx = _CHARSET.find(ch)
        if idx < 0:
            raise Bolt11DecodeError(f"Invalid bech32 character: {ch!r}")
        data5.append(idx)

    # Strip 6-char checksum (30 bits) and 65-byte signature (104 x 5-bit groups)
    # Order: [timestamp 7 groups] [tagged fields ...] [signature 104 groups] [checksum 6 groups]
    if len(data5) < 7 + _SIG_DATA5_LEN + 6:
        raise Bolt11DecodeError("Data section too short after stripping overhead")

    # Checksum is last 6 chars; signature is 104 groups before that.
    payload5 = data5[: -(6 + _SIG_DATA5_LEN)]  # strip signature + checksum
    if len(payload5) < 7:
        raise Bolt11DecodeError("Payload too short (no room for timestamp)")

    # -----------------------------------------------------------------------
    # 3. Convert the entire payload to a bitstream (easier to slice tags)
    # -----------------------------------------------------------------------
    bits = _to_bits(payload5)

    # -----------------------------------------------------------------------
    # 4. Parse HRP for amount
    # -----------------------------------------------------------------------
    amount_msat = _parse_hrp_amount(hrp)

    # -----------------------------------------------------------------------
    # 5. Timestamp — first 35 bits
    # -----------------------------------------------------------------------
    if len(bits) < 35:
        raise Bolt11DecodeError("Not enough bits for timestamp")
    timestamp = _bits_to_int(bits[:35])
    pos = 35

    # -----------------------------------------------------------------------
    # 6. Tagged fields
    # -----------------------------------------------------------------------
    payment_hash_hex: str | None = None
    expiry = 3600  # BOLT-11 spec default
    payee_pubkey_hex: str | None = None
    description: str | None = None

    while pos + 15 <= len(bits):  # need at least tag(5) + length(10)
        tag = _bits_to_int(bits[pos : pos + 5])
        length_5 = _bits_to_int(bits[pos + 5 : pos + 15])
        pos += 15
        value_bits = bits[pos : pos + length_5 * 5]
        pos += length_5 * 5

        if len(value_bits) < length_5 * 5:
            break  # truncated; stop gracefully

        if tag == _TAG_PAYMENT_HASH and length_5 == 52:
            payment_hash_hex = _bits_to_bytes(value_bits, 32).hex()

        elif tag == _TAG_EXPIRY:
            expiry = _bits_to_int(value_bits)

        elif tag == _TAG_PAYEE_PUBKEY and length_5 == 53:
            payee_pubkey_hex = _bits_to_bytes(value_bits, 33).hex()

        elif tag == _TAG_DESCRIPTION:
            raw = _bits_to_bytes(value_bits, len(value_bits) // 8)
            try:
                description = raw.decode("utf-8", errors="replace")
            except Exception:
                pass

    if payment_hash_hex is None:
        raise Bolt11DecodeError("BOLT-11 invoice missing required payment_hash tag")

    return DecodedBolt11(
        amount_msat=amount_msat,
        payment_hash_hex=payment_hash_hex,
        expiry=expiry,
        timestamp=timestamp,
        payee_pubkey_hex=payee_pubkey_hex,
        description=description,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _to_bits(data5: list[int]) -> list[int]:
    """Convert a list of 5-bit groups into a flat list of individual bits."""
    bits: list[int] = []
    for v in data5:
        for shift in (4, 3, 2, 1, 0):
            bits.append((v >> shift) & 1)
    return bits


def _bits_to_int(bits: list[int]) -> int:
    """Interpret a list of bits as a big-endian unsigned integer."""
    result = 0
    for b in bits:
        result = (result << 1) | b
    return result


def _bits_to_bytes(bits: list[int], n_bytes: int) -> bytes:
    """Convert bits to exactly n_bytes, discarding trailing padding bits."""
    result = bytearray()
    for i in range(n_bytes):
        byte = 0
        for shift in range(8):
            idx = i * 8 + shift
            if idx < len(bits):
                byte = (byte << 1) | bits[idx]
            else:
                byte <<= 1
        result.append(byte)
    return bytes(result)


def _parse_hrp_amount(hrp: str) -> int | None:
    """Parse the amount from the BOLT-11 human-readable prefix.

    Examples:
        "lnbc"        → None (no amount specified)
        "lnbc500n"    → 50_000 (msat)  — 500 nano-BTC
        "lnbc10m"     → 1_000_000_000 (msat) — 10 milli-BTC
        "lnbcrt1000u" → 100_000_000 (msat) — regtest 1000 micro-BTC

    Returns:
        Amount in millisatoshi, or None for zero-amount invoices.
    """
    # Longer prefixes first to avoid "lnbc" matching "lnbcrt" prematurely.
    for prefix in ("lnbcrt", "lntbs", "lntb", "lnbc"):
        if hrp.startswith(prefix):
            tail = hrp[len(prefix) :]
            break
    else:
        return None  # unrecognised prefix; caller will raise if needed

    if not tail:
        return None  # no amount (e.g. pure "lnbc")

    # Last char might be a multiplier letter
    if tail[-1].isalpha():
        mult_char = tail[-1]
        digits = tail[:-1]
    else:
        mult_char = ""
        digits = tail

    if not digits.isdigit():
        return None

    amount = int(digits)
    if not mult_char:
        # Amount is in whole BTC — convert to msat (1 BTC = 10^11 msat)
        return amount * 100_000_000_000

    mult = _MULT_MSAT.get(mult_char)
    if mult is None:
        # "p" (pico) gives sub-millisat amounts — treat as None / unsupported
        return None

    return amount * mult
