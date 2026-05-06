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
import logging
import re
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

import httpx

from routeweiler._base import RouteweilerLooseModel
from routeweiler._constants import HTTP_STATUS_PAYMENT_REQUIRED
from routeweiler.errors import (
    ChallengeExpiredError,
    ChallengeParseError,
    MppReceiptVerificationError,
)

if TYPE_CHECKING:
    from routeweiler.rails.base import PaymentResult, SettlementInfo

_log = logging.getLogger(__name__)

WWW_AUTHENTICATE = "www-authenticate"
AUTHORIZATION = "authorization"
PAYMENT_RECEIPT = "payment-receipt"  # lowercase for httpx header lookup
PAYMENT_SCHEME = "Payment"  # the auth-scheme name in both headers


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
        raise TypeError(
            "JCS: float values are not supported in MPP receipts; use integer minor units"
        )
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


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(s: str) -> bytes:
    # Add padding if needed
    pad = 4 - len(s) % 4
    if pad != 4:
        s += "=" * pad
    return base64.urlsafe_b64decode(s)


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
    if not stripped.lower().startswith("payment "):
        raise ValueError(
            f"WWW-Authenticate header does not begin with 'Payment ': {header_value!r}"
        )
    stripped = stripped[len("payment ") :]

    result: dict[str, str] = {}
    for m in _AUTH_PARAM_RE.finditer(stripped):
        key = m.group(1).lower()
        # quoted-string value (group 2) or token value (group 3)
        raw_value = m.group(2) if m.group(2) is not None else m.group(3)
        # Unescape \" and \\ in quoted strings
        value = raw_value.replace('\\"', '"').replace("\\\\", "\\")
        result[key] = value
    return result


def decode_request_param(b64url: str) -> dict[str, Any]:
    """Decode a base64url JCS-JSON ``request`` auth-param."""
    try:
        raw = b64url_decode(b64url)
        result: dict[str, Any] = json.loads(raw)
        return result
    except Exception as exc:
        raise ValueError(f"MPP request param decode failed: {exc}") from exc


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


class MppReceipt(RouteweilerLooseModel):
    """Decoded ``Payment-Receipt`` header payload."""

    challenge_id: str
    method: Literal["tempo", "stripe", "lightning", "solana", "stellar", "monad", "card"]
    reference: str
    settlement: dict[str, str]
    status: Literal["success", "failure"]
    timestamp: datetime


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


# Fallback when the server omits the `expires` auth-param.  The MPP spec treats
# `expires` as REQUIRED, but real deployments occasionally omit it.  We apply a
# 5-minute window rather than raising ChallengeParseError so clients stay
# resilient against non-conformant servers until stricter enforcement is needed.
_DEFAULT_VALIDITY_SECONDS = 300


def parse_mpp_envelope(
    header_value: str,
    *,
    rail_prefix: str,
) -> tuple[str, dict[str, Any], dict[str, str]]:
    """Parse a ``WWW-Authenticate: Payment`` header and decode the ``request`` param.

    Returns ``(challenge_id, request_dict, params)``.

    Raises ``ChallengeParseError`` on any failure.
    """
    try:
        params = parse_payment_challenge(header_value)
    except Exception as exc:
        _log.warning("%s: malformed WWW-Authenticate: %s", rail_prefix, exc)
        raise ChallengeParseError(f"{rail_prefix}: malformed WWW-Authenticate: {exc}") from exc

    challenge_id = params.get("id", "")
    if not challenge_id:
        raise ChallengeParseError(f"{rail_prefix}: missing 'id' auth-param")

    request_b64 = params.get("request", "")
    if not request_b64:
        raise ChallengeParseError(f"{rail_prefix}: missing 'request' auth-param")
    try:
        req: dict[str, Any] = decode_request_param(request_b64)
    except Exception as exc:
        raise ChallengeParseError(f"{rail_prefix}: failed to decode 'request': {exc}") from exc

    return challenge_id, req, params


def compute_mpp_expiry(
    params: dict[str, str],
    challenge_id: str,
    *,
    rail_prefix: str,
    default_seconds: int = _DEFAULT_VALIDITY_SECONDS,
) -> datetime:
    """Parse the ``expires`` auth-param or fall back to now + ``default_seconds``.

    Raises ``ChallengeParseError`` on bad format; ``ChallengeExpiredError`` if past.
    """
    expires_str = params.get("expires", "")
    if expires_str:
        try:
            expires_at = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ChallengeParseError(
                f"{rail_prefix}: could not parse 'expires' value {expires_str!r}: {exc}"
            ) from exc
    else:
        expires_at = datetime.fromtimestamp(time.time() + default_seconds, tz=UTC)

    if datetime.now(tz=UTC) >= expires_at:
        raise ChallengeExpiredError(
            f"{rail_prefix} challenge {challenge_id!r} expired at {expires_at.isoformat()}"
        )

    return expires_at


def build_mpp_challenge_echo(
    challenge_id: str,
    auth_params: dict[str, Any],
    *,
    default_method: str,
) -> dict[str, Any]:
    """Build the challenge echo dict for an MPP credential, stripping empty-string keys."""
    return {
        k: v
        for k, v in (
            ("id", challenge_id),
            ("realm", auth_params.get("realm", "")),
            ("method", auth_params.get("method", default_method)),
            ("intent", auth_params.get("intent", "charge")),
            ("request", auth_params.get("request", "")),
            ("expires", auth_params.get("expires", "")),
            ("opaque", auth_params.get("opaque", "")),
        )
        if v != ""
    }


def confirm_mpp_receipt(
    result: PaymentResult,
    response: httpx.Response,
    *,
    expected_methods: set[str],
    network_id: str,
    rail_prefix: str,
    facilitator: str | None = None,
) -> SettlementInfo:
    """Parse the ``Payment-Receipt`` header and return a ``SettlementInfo``.

    Returns a minimal ``SettlementInfo`` when no receipt header is present.

    Raises ``MppReceiptVerificationError`` on decode failure or mismatched fields.
    """
    from routeweiler.rails.base import SettlementInfo  # noqa: PLC0415

    receipt_header = response.headers.get(PAYMENT_RECEIPT, "")
    if not receipt_header:
        return SettlementInfo(
            success=response.is_success,
            tx_hash=result.proof_value,
            network_id=network_id,
            payer_address=None,
            amount_paid=None,
            facilitator=facilitator,
        )

    try:
        receipt = parse_payment_receipt(receipt_header)
    except Exception as exc:
        raise MppReceiptVerificationError(
            f"{rail_prefix}: failed to decode Payment-Receipt: {exc}"
        ) from exc

    expected_id = (result.credential or {}).get("charge_id", "")
    if expected_id and receipt.challenge_id != expected_id:
        raise MppReceiptVerificationError(
            f"{rail_prefix}: receipt challengeId {receipt.challenge_id!r} != "
            f"expected {expected_id!r}"
        )
    if receipt.method not in expected_methods:
        raise MppReceiptVerificationError(
            f"{rail_prefix}: receipt method {receipt.method!r} not in {expected_methods!r}"
        )

    try:
        amount_paid = int(receipt.settlement.get("amount", "0"))
    except (ValueError, TypeError):
        amount_paid = None

    return SettlementInfo(
        success=receipt.status == "success" and response.is_success,
        tx_hash=receipt.reference,
        network_id=network_id,
        payer_address=None,
        amount_paid=amount_paid,
        facilitator=facilitator,
    )


def is_mpp_payment_for(response: httpx.Response, methods: set[str]) -> bool:
    """Return True when ``response`` is a 402 with a recognised MPP ``method`` value.

    Encapsulates the boilerplate shared by every MPP ``can_handle`` implementation:
    status-code check, header presence, ``Payment`` scheme prefix, and method match.
    ``methods`` should be lower-cased (e.g. ``{"tempo"}`` or ``{"stripe", "card"}``).
    """
    if response.status_code != HTTP_STATUS_PAYMENT_REQUIRED:
        return False
    header = response.headers.get(WWW_AUTHENTICATE, "")
    if not header or not header.strip().lower().startswith("payment"):
        return False
    try:
        params = parse_payment_challenge(header)
    except (ValueError, TypeError):
        return False
    return params.get("method", "").lower() in methods


def parse_required_request_fields(
    req: dict[str, Any],
    *,
    fields: tuple[str, ...],
    rail_label: str,
) -> None:
    """Raise ``ChallengeParseError`` when any of ``fields`` is absent from ``req``.

    Args:
        req:        Decoded ``request`` dict from the MPP challenge envelope.
        fields:     Required field names (e.g. ``("amount", "currency", "recipient")``).
        rail_label: Human-readable label used in the error message (e.g. ``"MPP-Tempo"``).
    """
    for field in fields:
        if field not in req:
            raise ChallengeParseError(f"{rail_label}: 'request' missing required field '{field}'")


def build_mpp_credential(
    *,
    challenge_id: str,
    auth_params: dict[str, Any],
    default_method: str,
    payload: dict[str, Any],
    source: str,
) -> tuple[dict[str, Any], str]:
    """Build the MPP credential dict and the corresponding ``Authorization`` header value.

    Both the Tempo and SPT adapters share this envelope structure; only ``payload``
    and ``source`` differ between them.

    Returns:
        A ``(credential_dict, header_value)`` tuple where ``header_value`` is the
        full ``Authorization: Payment <b64url>`` string ready to send.
    """
    credential: dict[str, Any] = {
        "challenge": build_mpp_challenge_echo(
            challenge_id, auth_params, default_method=default_method
        ),
        "payload": payload,
        "source": source,
    }
    return credential, build_authorization_header(credential)
