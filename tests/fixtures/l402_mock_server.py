"""In-process ASGI mock L402 server for integration testing.

Exposes two routes:
    GET /protected   — returns 402 on first visit; 200 on valid L402 retry.
    GET /free        — always returns 200 (passthrough test helper).

Mounted via ``httpx.ASGITransport(app=mock_l402_app)`` in test fixtures.
No subprocess, no port binding, no real Lightning node.

The mock server validates:
    - Authorization: L402 <macaroon>:<preimage> header is present.
    - sha256(preimage) == payment_hash embedded in the synthetic BOLT-11.
"""

from __future__ import annotations

import base64
import hashlib

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

# ---------------------------------------------------------------------------
# Build helpers — only used at module load time to assemble test fixtures
# ---------------------------------------------------------------------------


def _int_to_5bit_be(value: int, n_groups: int) -> list[int]:
    """Encode an integer into n_groups big-endian 5-bit groups."""
    groups: list[int] = []
    for _ in range(n_groups):
        groups.append(value & 0x1F)
        value >>= 5
    return list(reversed(groups))


def _bytes_to_5bit(data: bytes) -> list[int]:
    """Convert bytes to 5-bit groups, padding the last group with zero bits."""
    bits: list[int] = []
    for byte in data:
        for shift in (7, 6, 5, 4, 3, 2, 1, 0):
            bits.append((byte >> shift) & 1)
    while len(bits) % 5:
        bits.append(0)
    groups: list[int] = []
    for i in range(0, len(bits), 5):
        v = 0
        for b in bits[i : i + 5]:
            v = (v << 1) | b
        groups.append(v)
    return groups


def _build_mock_invoice(payment_hash_hex: str) -> str:
    """Build a minimal synthetic BOLT-11 regtest invoice.

    Encodes the given payment_hash into the invoice's ``p`` tag so that
    ``_bolt11.decode()`` reads back the correct hash.  The invoice is NOT
    cryptographically valid (signature is zeroed) — our decoder deliberately
    skips signature verification.

    Amount: 50000n = 50 sats (50_000 msat), regtest prefix ``lnbcrt``.
    Expiry: effectively forever (99_999_999 seconds).
    Timestamp: fixed at 1_700_000_000 (2023-11-14).
    """
    charset = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"

    # --- Timestamp (35 bits = 7 x 5-bit groups) ---
    ts_groups = _int_to_5bit_be(1_700_000_000, 7)

    # --- Tag p: payment_hash (type=1, length=52, value=52 x 5-bit groups) ---
    hash_5bit = _bytes_to_5bit(bytes.fromhex(payment_hash_hex))[:52]
    tag_p = [1, *_int_to_5bit_be(52, 2), *hash_5bit]

    # --- Tag x: expiry (type=6, length=6, value=6 x 5-bit groups = 30 bits) ---
    # 99_999_999 seconds ~3.2 years; needs 27 bits -> 6 groups (30 bits) is sufficient.
    expiry_groups = _int_to_5bit_be(99_999_999, 6)
    tag_x = [6, *_int_to_5bit_be(6, 2), *expiry_groups]

    payload = ts_groups + tag_p + tag_x
    # Signature placeholder (104 x 5-bit groups) + checksum placeholder (6)
    all_groups = payload + [0] * 104 + [0] * 6
    data_str = "".join(charset[g] for g in all_groups)
    return f"lnbcrt50000n1{data_str}"


# ---------------------------------------------------------------------------
# Deterministic test fixtures
# ---------------------------------------------------------------------------

# Fixed 32-byte preimage — DO NOT USE WITH REAL FUNDS.
MOCK_PREIMAGE: bytes = bytes.fromhex(
    "0102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f20"
)
MOCK_PAYMENT_HASH: str = hashlib.sha256(MOCK_PREIMAGE).hexdigest()

# Synthetic BOLT-11 invoice whose p-tag encodes MOCK_PAYMENT_HASH
MOCK_BOLT11: str = _build_mock_invoice(MOCK_PAYMENT_HASH)

# Synthetic macaroon (valid base64, not a real HMAC chain)
MOCK_MACAROON_B64: str = base64.b64encode(b"mock-macaroon-" + MOCK_PAYMENT_HASH.encode()).decode()

MOCK_WWW_AUTHENTICATE: str = f'L402 macaroon="{MOCK_MACAROON_B64}", invoice="{MOCK_BOLT11}"'

# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def protected(request: Request) -> Response:
    auth_header = request.headers.get("Authorization", "")

    if not auth_header.lower().startswith("l402 "):
        return Response(
            content=b"payment required",
            status_code=402,
            headers={"WWW-Authenticate": MOCK_WWW_AUTHENTICATE},
        )

    # Validate: Authorization: L402 <macaroon>:<preimage_hex>
    try:
        _, credential = auth_header.split(" ", 1)
        mac, preimage_hex = credential.rsplit(":", 1)
        preimage_bytes = bytes.fromhex(preimage_hex)
        actual_hash = hashlib.sha256(preimage_bytes).hexdigest()
        if actual_hash != MOCK_PAYMENT_HASH:
            raise ValueError(f"preimage hash mismatch: {actual_hash!r} != {MOCK_PAYMENT_HASH!r}")
        if mac != MOCK_MACAROON_B64:
            raise ValueError(f"macaroon mismatch: {mac!r}")
    except Exception as exc:
        return Response(
            content=f"invalid L402 credential: {exc}".encode(),
            status_code=401,
        )

    return JSONResponse({"result": "ok", "rail": "l402"})


async def free(request: Request) -> Response:
    return JSONResponse({"free": True})


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

mock_l402_app = Starlette(
    routes=[
        Route("/protected", protected),
        Route("/free", free),
    ]
)
