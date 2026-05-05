"""MPP HTTP-authentication scheme helpers.

Implements the wire-format layer for the Machine Payments Protocol
(IETF ``draft-httpauth-payment-00``, https://paymentauth.org).

Headers:
    402 challenge:    ``WWW-Authenticate: Payment <auth-params>``
    Retry:            ``Authorization: Payment <b64url(JCS-JSON credential)>``
    Settlement proof: ``Payment-Receipt: <b64url(JCS-JSON receipt)>``

``request`` and ``opaque`` auth-params carry base64url-encoded JCS-JSON.
The credential blob is itself base64url-encoded JCS-JSON.

JCS (RFC 8785) canonicalisation is implemented inline — the library footprint
does not justify a dependency for a 60-line deterministic JSON serialiser.

All functions are pure (no I/O) and side-effect-free so they are trivially
unit-testable.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import field_validator

from routewiler._base import RoutewilerModel

# ---------------------------------------------------------------------------
# Wire-format header constants
# ---------------------------------------------------------------------------

WWW_AUTHENTICATE = "www-authenticate"  # lowercase for httpx header lookup
AUTHORIZATION = "Authorization"
PAYMENT_RECEIPT = "payment-receipt"  # lowercase for httpx header lookup
PAYMENT_SCHEME = "Payment"  # the auth-scheme name in both headers

# ---------------------------------------------------------------------------
# JCS canonicalisation (RFC 8785, §3.2)
# ---------------------------------------------------------------------------

# Regex to detect whether a float needs special handling
# (we only ever produce ints/strings/booleans/null/dicts/lists here, but be safe)
_FLOAT_NEEDS_CARE = re.compile(r"[.eE]")


def _jcs_serialize(obj: Any) -> str:
    """Deterministically serialise ``obj`` per RFC 8785 (JCS).

    Supported types: dict, list, str, int, float, bool, None.
    Dicts are serialised with keys sorted lexicographically (Unicode codepoint
    order, which is what Python's default sort provides for str).

    This is sufficient for the MPP credential payload which carries only
    string keys and JSON-primitive values.
    """
    if obj is None:
        return "null"
    if isinstance(obj, bool):
        return "true" if obj else "false"
    if isinstance(obj, int):
        return str(obj)
    if isinstance(obj, float):
        # JCS requires IEEE 754 round-trip encoding; use repr for Python floats.
        return repr(obj)
    if isinstance(obj, str):
        # Minimal JSON string encoding (escape only what JSON requires).
        escaped = (
            obj.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\b", "\\b")
            .replace("\f", "\\f")
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )
        # Control characters U+0000–U+001F that aren't the above
        result = []
        for ch in escaped:
            cp = ord(ch)
            if cp < 0x20 and ch not in ("\\b", "\\f", "\\n", "\\r", "\\t"):
                result.append(f"\\u{cp:04x}")
            else:
                result.append(ch)
        return '"' + "".join(result) + '"'
    if isinstance(obj, dict):
        pairs = ",".join(f"{_jcs_serialize(k)}:{_jcs_serialize(v)}" for k, v in sorted(obj.items()))
        return "{" + pairs + "}"
    if isinstance(obj, list):
        return "[" + ",".join(_jcs_serialize(item) for item in obj) + "]"
    raise TypeError(f"JCS: unsupported type {type(obj)!r}")


def jcs_encode(obj: dict[str, Any]) -> bytes:
    """Serialise ``obj`` to JCS-canonical UTF-8 bytes."""
    return _jcs_serialize(obj).encode("utf-8")


# ---------------------------------------------------------------------------
# Base64url helpers (no padding, RFC 4648 §5)
# ---------------------------------------------------------------------------


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(s: str) -> bytes:
    # Add padding if needed
    pad = 4 - len(s) % 4
    if pad != 4:
        s += "=" * pad
    return base64.urlsafe_b64decode(s)


# ---------------------------------------------------------------------------
# Auth-param parser (RFC 7235 / RFC 9110 §11.2)
# ---------------------------------------------------------------------------

# Quoted-string or token parser for WWW-Authenticate auth-params.
# Handles: key="value with spaces", key=token, key="val\"escaped"
_AUTH_PARAM_RE = re.compile(
    r'([A-Za-z][A-Za-z0-9\-]*)=(?:"((?:[^"\\]|\\.)*)"|([^\s,]+))',
)


def parse_payment_challenge(header_value: str) -> dict[str, str]:
    """Parse a ``WWW-Authenticate: Payment ...`` header into a key-value dict.

    Strips the ``Payment`` scheme prefix and returns all auth-params.

    Args:
        header_value:  The full header value, e.g.
                       ``Payment id="abc", method="tempo", request="eyJ..."``.

    Returns:
        Dict of param names (lowercase) → unescaped string values.

    Raises:
        ValueError: if the header doesn't begin with the ``Payment`` scheme.
    """
    stripped = header_value.strip()
    # Accept both "Payment ..." and bare params (for robustness in tests)
    if stripped.lower().startswith("payment "):
        stripped = stripped[len("payment ") :]
    elif stripped.lower().startswith("payment"):
        stripped = stripped[len("payment") :]

    result: dict[str, str] = {}
    for m in _AUTH_PARAM_RE.finditer(stripped):
        key = m.group(1).lower()
        # quoted-string value (group 2) or token value (group 3)
        raw_value = m.group(2) if m.group(2) is not None else m.group(3)
        # Unescape \" and \\ in quoted strings
        value = raw_value.replace('\\"', '"').replace("\\\\", "\\")
        result[key] = value
    return result


# ---------------------------------------------------------------------------
# Request-param helpers
# ---------------------------------------------------------------------------


def decode_request_param(b64url: str) -> dict[str, Any]:
    """Decode a base64url JCS-JSON ``request`` auth-param."""
    try:
        raw = b64url_decode(b64url)
        result: dict[str, Any] = json.loads(raw)
        return result
    except Exception as exc:
        raise ValueError(f"MPP request param decode failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Credential builder
# ---------------------------------------------------------------------------


def encode_credential(credential: dict[str, Any]) -> str:
    """JCS-canonicalise ``credential`` and return the base64url-encoded string.

    This is the token that appears after ``Authorization: Payment `` on the
    retry request.
    """
    canonical = jcs_encode(credential)
    return b64url_encode(canonical)


def build_authorization_header(credential: dict[str, Any]) -> str:
    """Return the full ``Authorization`` header value for an MPP retry.

    Format: ``Payment <b64url(JCS-JSON credential)>``
    """
    return f"{PAYMENT_SCHEME} {encode_credential(credential)}"


# ---------------------------------------------------------------------------
# Receipt model and parser
# ---------------------------------------------------------------------------


class MppReceipt(RoutewilerModel):
    """Decoded ``Payment-Receipt`` header payload."""

    challenge_id: str
    method: Literal["tempo", "stripe", "lightning", "solana", "stellar", "monad", "card"]
    reference: str
    settlement: dict[str, str]
    status: Literal["success", "failure"]
    timestamp: datetime

    @field_validator("timestamp", mode="before")
    @classmethod
    def _parse_timestamp(cls, v: Any) -> Any:
        if isinstance(v, str):
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        return v


def parse_payment_receipt(header_value: str) -> MppReceipt:
    """Decode a ``Payment-Receipt`` header into a validated ``MppReceipt``.

    Args:
        header_value:  Raw header value (base64url-encoded JCS-JSON).

    Returns:
        A validated ``MppReceipt`` instance.

    Raises:
        ValueError:       Base64 or JSON decode failure.
        ValidationError:  Pydantic validation failure (missing/wrong fields).
    """
    try:
        raw = b64url_decode(header_value.strip())
        data: dict[str, Any] = json.loads(raw)
    except Exception as exc:
        raise ValueError(f"Payment-Receipt decode failed: {exc}") from exc
    return MppReceipt.model_validate(data)


def build_payment_receipt(
    *,
    challenge_id: str,
    method: str,
    reference: str,
    amount: str,
    currency: str,
    status: str = "success",
) -> str:
    """Build a base64url-encoded ``Payment-Receipt`` header value.

    Used by the mock server to construct synthetic receipt headers.
    """
    now = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
    receipt: dict[str, Any] = {
        "challengeId": challenge_id,
        "method": method,
        "reference": reference,
        "settlement": {"amount": amount, "currency": currency},
        "status": status,
        "timestamp": now,
    }
    return b64url_encode(jcs_encode(receipt))
