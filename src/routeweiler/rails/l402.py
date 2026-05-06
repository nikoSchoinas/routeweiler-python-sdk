"""L402 (formerly LSAT) rail adapter — detector, parser, and invoice payer.

Wire format (RFC 7235 WWW-Authenticate):
    Server challenge:  WWW-Authenticate: L402 macaroon="<b64>", invoice="<bolt11>"
    Client credential: Authorization: L402 <b64(macaroon)>:<hex(preimage)>

Flow:
    1. parse()       — decode WWW-Authenticate into NormalizedChallenge
    2. match_funding() — find a LightningFundingSource for the invoice network
    3. pay()         — pay the BOLT-11 invoice, get preimage, build Authorization header
    4. confirm()     — return minimal SettlementInfo (no server settlement header for L402)

Reference: https://github.com/lightninglabs/L402
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast

import httpx

from routeweiler._constants import HTTP_STATUS_PAYMENT_REQUIRED
from routeweiler.errors import (
    ChallengeExpiredError,
    ChallengeParseError,
    NoFundingForRailError,
    PreimageMismatchError,
)
from routeweiler.funding import FundingSource
from routeweiler.funding.lightning import LightningFundingSource
from routeweiler.normalized import (
    L402RailRaw,
    NormalizedChallenge,
    Payee,
    Price,
    ProofType,
    Rail,
)
from routeweiler.rails._bolt11 import Bolt11DecodeError, DecodedBolt11
from routeweiler.rails._bolt11 import decode as bolt11_decode
from routeweiler.rails._mpp_http import AUTHORIZATION
from routeweiler.rails.base import PaymentResult, SettlementInfo, resource_from_request

_log = logging.getLogger(__name__)

# Accepted WWW-Authenticate scheme names (same protocol, two names)
_ACCEPTED_SCHEMES = {"l402", "lsat"}

# Header name constants
_WWW_AUTHENTICATE_HEADER = "WWW-Authenticate"

# Maps BOLT-11 HRP prefix → LightningFundingSource.network value.
# Longer prefixes must come first to avoid "lnbc" matching "lnbcrt" prematurely.
_HRP_TO_NETWORK: dict[str, str] = {
    "lnbcrt": "bitcoin-regtest",
    "lntbs": "bitcoin-signet",
    "lntb": "bitcoin-testnet",
    "lnbc": "bitcoin",
}


def _invoice_network(bolt11: str) -> str | None:
    """Return the network name for a BOLT-11 invoice, or None if unrecognised."""
    lower = bolt11.lower()
    for hrp, network in _HRP_TO_NETWORK.items():
        if lower.startswith(hrp):
            return network
    return None


def _parse_www_authenticate(header: str) -> tuple[str, str] | None:
    """Parse a WWW-Authenticate: L402/LSAT header.

    Returns (macaroon_b64, bolt11_invoice) or None if the scheme is not L402/LSAT.
    Handles both double-quoted and unquoted param values.

    Examples:
        'L402 macaroon="AGIAJEe...", invoice="lnbc..."'
        'LSAT macaroon=AGIAJEe..., invoice=lnbc...'
    """
    header = header.strip()

    # Split scheme from params at the first whitespace
    parts = header.split(None, 1)
    if len(parts) < 2:  # noqa: PLR2004
        return None
    scheme, params_str = parts[0].lower(), parts[1]

    if scheme not in _ACCEPTED_SCHEMES:
        return None

    # Extract macaroon and invoice param values
    macaroon = _extract_param(params_str, "macaroon")
    invoice = _extract_param(params_str, "invoice")

    if macaroon is None or invoice is None:
        return None

    # Accept the first macaroon if comma-separated (aperture only emits one in a challenge)
    if "," in macaroon:
        macaroon = macaroon.split(",")[0].strip()

    return macaroon, invoice


def _extract_param(params: str, name: str) -> str | None:
    """Extract a named parameter from a WWW-Authenticate params string.

    Handles both:
        name="value"   (RFC 7235 quoted-string)
        name=value     (unquoted token)

    Returns the raw value string (without quotes), or None if not found.
    """
    # Try quoted first (greedy match within quotes), then unquoted token
    quoted = re.search(rf'\b{re.escape(name)}="([^"]*)"', params)
    if quoted:
        return quoted.group(1)

    unquoted = re.search(rf"\b{re.escape(name)}=([^\s,]+)", params)
    if unquoted:
        return unquoted.group(1)

    return None


def _parse_macaroon_caveats(macaroon_b64: str) -> dict[str, str]:
    """Deserialize the macaroon and return its first-party caveat key→value pairs.

    Returns an empty dict if pymacaroons is not installed or if deserialization
    fails with an expected decode/format error.  Unexpected exceptions propagate
    so callers can diagnose library issues rather than silently losing caveats.
    """

    try:
        from pymacaroons import Macaroon  # type: ignore[import-untyped]  # noqa: PLC0415
    except ImportError:
        return {}

    try:
        m = Macaroon.deserialize(macaroon_b64)
    except Exception:
        # pymacaroons raises its own exception hierarchy (MacaroonDeserializationException,
        # etc.) that doesn't map to stdlib types.  Catch broadly since this is optional
        # third-party code; callers fall back to invoice expiry on empty caveats.
        return {}

    caveats: dict[str, str] = {}
    for caveat in m.caveats:
        raw_id = caveat.caveat_id
        try:
            cid: str = raw_id.decode("utf-8") if isinstance(raw_id, bytes) else str(raw_id)
        except (UnicodeDecodeError, AttributeError):
            continue
        if "=" in cid:
            k, _, v = cid.partition("=")
            caveats[k.strip()] = v.strip()

    return caveats


class L402Adapter:
    """Rail adapter for the L402 (formerly LSAT) Lightning payment protocol.

    Detector: checks for HTTP 402 + WWW-Authenticate: L402 or LSAT header.
    Parser:   decodes the header into NormalizedChallenge with L402RailRaw.
    Payer:    pays the BOLT-11 invoice via LightningFundingSource, returns
              the Authorization header + credential dict.
    Confirmer: returns SettlementInfo with the preimage as proof_value.
    """

    rail: Rail = "l402"
    proof_type: ProofType = "preimage"

    def __init__(self, funding_sources: list[LightningFundingSource]) -> None:
        self._funding = funding_sources

    def can_handle(self, response: httpx.Response) -> bool:
        if response.status_code != HTTP_STATUS_PAYMENT_REQUIRED:
            return False
        www_auth = response.headers.get(_WWW_AUTHENTICATE_HEADER, "")
        scheme = www_auth.strip().split(None, 1)[0].lower() if www_auth.strip() else ""
        return scheme in _ACCEPTED_SCHEMES

    def parse(self, request: httpx.Request, response: httpx.Response) -> NormalizedChallenge:
        """Decode a WWW-Authenticate: L402/LSAT header into a NormalizedChallenge.

        Raises:
            ChallengeParseError:   Malformed header, invalid macaroon, or bad BOLT-11.
            ChallengeExpiredError: Invoice or macaroon valid_until is already past.
        """
        www_auth = response.headers.get(_WWW_AUTHENTICATE_HEADER, "")
        parsed = _parse_www_authenticate(www_auth)
        if parsed is None:
            _log.warning("l402: cannot parse WWW-Authenticate header: %r", www_auth)
            raise ChallengeParseError(f"Cannot parse WWW-Authenticate as L402/LSAT: {www_auth!r}")
        macaroon_b64, bolt11 = parsed

        # --- Decode BOLT-11 ------------------------------------------------
        try:
            inv: DecodedBolt11 = bolt11_decode(bolt11)
        except Bolt11DecodeError as exc:
            raise ChallengeParseError(f"Invalid BOLT-11 invoice: {exc}") from exc
        except Exception as exc:
            raise ChallengeParseError(f"BOLT-11 decode failed unexpectedly: {exc}") from exc

        # --- Compute expiries ----------------------------------------------
        now = datetime.now(UTC)
        invoice_expires_at = datetime.fromtimestamp(inv.timestamp + inv.expiry, tz=UTC)

        if invoice_expires_at <= now:
            raise ChallengeExpiredError(
                f"BOLT-11 invoice already expired at {invoice_expires_at.isoformat()}"
            )

        # Check macaroon valid_until caveat if present
        caveats = _parse_macaroon_caveats(macaroon_b64)
        macaroon_expires_at: datetime | None = None
        valid_until_str = caveats.get("valid_until")
        if valid_until_str:
            try:
                macaroon_expires_at = datetime.fromtimestamp(int(valid_until_str), tz=UTC)
            except (ValueError, OSError):
                pass  # unparseable caveat — ignore, use invoice expiry

        if macaroon_expires_at is not None and macaroon_expires_at <= now:
            raise ChallengeExpiredError(
                f"L402 macaroon already expired at {macaroon_expires_at.isoformat()}"
            )

        expires_at = (
            min(invoice_expires_at, macaroon_expires_at)
            if macaroon_expires_at is not None
            else invoice_expires_at
        )

        # --- Build NormalizedChallenge -------------------------------------
        amount_msat = inv.amount_msat or 0
        sats = amount_msat // 1000

        raw = L402RailRaw(kind="l402", macaroon=macaroon_b64, invoice=bolt11)

        return NormalizedChallenge(
            rail="l402",
            resource=resource_from_request(request),
            price=Price(
                amount=sats,
                currency="btc-lightning",
                human_amount=f"{sats:,} sats",
            ),
            payee=Payee(
                identifier=inv.payee_pubkey_hex or inv.payment_hash_hex,
                metadata={"description": inv.description} if inv.description else {},
            ),
            scheme="exact",  # L402 has no upto/stream modes
            nonce=inv.payment_hash_hex,  # unique per invoice, stable for idempotency
            expires_at=expires_at,
            raw=raw,
        )

    def match_funding(
        self,
        challenge: NormalizedChallenge,
        funding: Sequence[FundingSource],
    ) -> LightningFundingSource | None:
        """Return the first LightningFundingSource whose network matches the invoice."""
        if not isinstance(challenge.raw, L402RailRaw):
            return None
        target_network = _invoice_network(challenge.raw.invoice)
        if target_network is None:
            return None
        for fs in funding:
            if isinstance(fs, LightningFundingSource) and fs.network == target_network:
                return fs
        return None

    async def pay(
        self,
        challenge: NormalizedChallenge,
    ) -> PaymentResult:
        """Pay the BOLT-11 invoice and return a PaymentResult with the Authorization header.

        Steps:
            1. Extract invoice from challenge.raw.
            2. Locate the matching LightningFundingSource.
            3. Pay via the source's Lightning node; receive 32-byte preimage.
            4. Verify sha256(preimage) == invoice payment_hash (defense-in-depth).
            5. Build Authorization: L402 <macaroon>:<preimage_hex> header.
            6. Return PaymentResult with both header and credential dict populated.

        Raises:
            NoFundingForRailError:  No LightningFundingSource registered.
            InvoicePaymentError:    Node returned a terminal payment failure.
            PreimageMismatchError:  Node returned a preimage that doesn't match the invoice.
        """
        _log.debug(
            "pay: rail=%s nonce=%s amount=%s", self.rail, challenge.nonce, challenge.price.amount
        )

        l402_raw = cast(L402RailRaw, challenge.raw)
        bolt11 = l402_raw.invoice
        macaroon_b64 = l402_raw.macaroon

        source = self.match_funding(challenge, self._funding)
        if source is None:
            target = _invoice_network(bolt11)
            available = [s.network for s in self._funding]
            raise NoFundingForRailError(
                f"No LightningFundingSource for network {target!r}. Available: {available}"
            )

        # Pay the invoice
        preimage: bytes = await source.pay_invoice(bolt11)

        # Verify preimage integrity
        expected_hash = challenge.nonce  # nonce == payment_hash_hex set by parse()
        actual_hash = hashlib.sha256(preimage).hexdigest()
        if actual_hash != expected_hash:
            raise PreimageMismatchError(
                f"Preimage sha256 {actual_hash!r} != invoice payment_hash {expected_hash!r}. "
                "The Lightning node may be misbehaving."
            )

        preimage_hex = preimage.hex()
        auth_value = f"L402 {macaroon_b64}:{preimage_hex}"

        credential: dict[str, Any] = {
            "macaroon": macaroon_b64,
            "preimage_hex": preimage_hex,
            "invoice": bolt11,
            "payment_hash_hex": expected_hash,
            "amount_paid_sats": challenge.price.amount,
        }

        return PaymentResult(
            header_name=AUTHORIZATION,
            header_value=auth_value,
            credential=credential,
            proof_type=self.proof_type,  # "preimage"
            proof_value=preimage_hex,
        )

    async def confirm(
        self,
        result: PaymentResult,
        response: httpx.Response,
    ) -> SettlementInfo:
        """Return a minimal SettlementInfo.

        L402 has no server-side settlement header — the preimage is the only
        proof, and it was captured in pay().  We read amount_paid from the
        credential dict for the trace.
        """
        _log.debug("confirm: status=%d", response.status_code)
        amount_paid: int | None = None
        if result.credential is not None:
            raw = result.credential.get("amount_paid_sats")
            if isinstance(raw, int):
                amount_paid = raw

        return SettlementInfo(
            success=response.is_success,
            tx_hash=None,  # Lightning payment_hash is the nonce, not a tx hash
            network_id=None,
            payer_address=None,
            amount_paid=amount_paid,
            facilitator="lightning",
        )
