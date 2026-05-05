"""MPP-Tempo rail adapter — client-signed stablecoin payments on Tempo.

Implements ``RailAdapter`` for the MPP ``tempo`` charge method
(https://paymentauth.org/draft-tempo-charge-00.html, IETF draft).

Flow (pull mode — server broadcasts the signed transaction):
    1. ``can_handle``   — 402 with ``WWW-Authenticate: Payment method=tempo``.
    2. ``parse``        — decode auth-params + JCS-JSON request into
                          ``NormalizedChallenge``.
    3. ``match_funding``— find a ``TempoFundingSource`` whose chain ID and asset
                          match the challenge's methodDetails.chainId / currency.
    4. ``pay``          — sign a type-0x76 Tempo Transaction via the signer;
                          wrap in MPP credential; build ``Authorization: Payment``.
    5. ``confirm``      — decode ``Payment-Receipt`` header; return
                          ``SettlementInfo`` with the on-chain tx hash.

Week 13 scope:
    - pull mode only (client signs, server broadcasts).
    - Single TIP-20 transfer per transaction.
    - Non-zero amounts only (zero-amount proof path is a follow-up).
    - method=tempo only (method=stripe / method=card handled by MppSptAdapter
      in Week 14).
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx

from routewiler._constants import HTTP_STATUS_PAYMENT_REQUIRED
from routewiler.assets import ASSETS_BY_ADDRESS, CANONICAL_ADDRESSES, CHAIN_IDS
from routewiler.errors import (
    ChallengeExpiredError,
    ChallengeParseError,
    MppChargeFailedError,
    MppReceiptVerificationError,
    NoFundingForRailError,
)
from routewiler.funding.tempo import TempoFundingSource
from routewiler.normalized import (
    MppTempoRailRaw,
    NormalizedChallenge,
    Payee,
    Price,
    ProofType,
    Rail,
    Resource,
)
from routewiler.rails._mpp_http import (
    PAYMENT_RECEIPT,
    WWW_AUTHENTICATE,
    build_authorization_header,
    decode_request_param,
    parse_payment_challenge,
    parse_payment_receipt,
)
from routewiler.rails._tempo_tx import tx_hash as tempo_tx_hash
from routewiler.rails.base import PaymentResult, RailAdapter, SettlementInfo

if TYPE_CHECKING:
    from routewiler.budgets.schema import DrawReceipt
    from routewiler.funding import FundingSource

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WWW_AUTH_HEADER = WWW_AUTHENTICATE  # "www-authenticate" (httpx lower-cases)
_PAYMENT_RECEIPT_HEADER = PAYMENT_RECEIPT  # "payment-receipt"
_AUTHORIZATION_HEADER = "Authorization"

# Default nonce when the caller doesn't supply one (mock / offline tests).
_DEFAULT_NONCE = 0

# Default validity window: 5 minutes from signing time.
_DEFAULT_VALIDITY_SECONDS = 300

# Chain ID → Tempo network name (reverse of CHAIN_IDS).
_CHAIN_ID_TO_NETWORK: dict[int, str] = {v: k for k, v in CHAIN_IDS.items() if "tempo" in k}

# Currency string suffix used for Tempo TIP-20 tokens in Price.currency.
# Format: ``<contract_address>-tip20`` (CAIP-19-ish; distinguishes from ERC-20).
_TIP20_SUFFIX = "-tip20"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tip20_currency_string(contract: str) -> str:
    """Return the Price.currency string for a TIP-20 contract.

    Example: ``"0x20c0...0000-tip20"``
    The FMV module treats any ``<x>-tip20`` with a known peg as stablecoin_peg.
    """
    return contract.lower() + _TIP20_SUFFIX


def _human_amount(contract_addr: str, amount: int) -> str:
    """Best-effort human amount string for a TIP-20 token."""
    meta = ASSETS_BY_ADDRESS.get(contract_addr.lower())
    if meta is None:
        return f"{amount} units"
    decimals = meta.decimals
    human = amount / (10**decimals)
    symbol = meta.symbol
    return f"{human:.6g} {symbol}"


def _resolve_contract(network: str, asset: str) -> str | None:
    """Resolve a canonical asset name to a Tempo TIP-20 contract address.

    Returns None if the asset is already a hex address.
    """
    if asset.startswith("0x"):
        return asset
    return CANONICAL_ADDRESSES.get((network, asset))


def _extract_address_from_did(source: str) -> str | None:
    """Extract the Ethereum address from a ``did:pkh:eip155:<chainId>:<address>`` DID."""
    try:
        parts = source.split(":")
        return parts[-1] if parts[-1].startswith("0x") else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class MppTempoAdapter:
    """MPP-Tempo rail adapter.

    Pass a list of ``TempoFundingSource`` objects (one per supported network /
    asset combination).  The adapter picks the best match for each challenge
    in ``match_funding``.
    """

    rail: Rail = "mpp-tempo"
    proof_type: ProofType = "txid"

    def __init__(self, funding_sources: list[TempoFundingSource]) -> None:
        self._funding = funding_sources

    # ------------------------------------------------------------------
    # RailAdapter protocol
    # ------------------------------------------------------------------

    def can_handle(self, response: httpx.Response) -> bool:
        """Return True for a 402 with ``WWW-Authenticate: Payment method=tempo``."""
        if response.status_code != HTTP_STATUS_PAYMENT_REQUIRED:
            return False
        header = response.headers.get(_WWW_AUTH_HEADER, "")
        if not header:
            return False
        # Must start with "Payment" scheme (case-insensitive)
        if not header.strip().lower().startswith("payment"):
            return False
        try:
            params = parse_payment_challenge(header)
        except Exception:
            return False
        return params.get("method", "").lower() == "tempo"

    def parse(self, request: httpx.Request, response: httpx.Response) -> NormalizedChallenge:
        """Decode the MPP-Tempo 402 challenge into a ``NormalizedChallenge``.

        Raises:
            ChallengeParseError:   Malformed header, missing required fields.
            ChallengeExpiredError: Challenge ``expires`` is in the past.
        """
        header = response.headers.get(_WWW_AUTH_HEADER, "")
        try:
            params = parse_payment_challenge(header)
        except Exception as exc:
            raise ChallengeParseError(f"MPP-Tempo: malformed WWW-Authenticate: {exc}") from exc

        challenge_id = params.get("id", "")
        if not challenge_id:
            raise ChallengeParseError("MPP-Tempo: missing 'id' auth-param")

        # Decode the base64url JCS-JSON ``request`` param
        request_b64 = params.get("request", "")
        if not request_b64:
            raise ChallengeParseError("MPP-Tempo: missing 'request' auth-param")
        try:
            req = decode_request_param(request_b64)
        except Exception as exc:
            raise ChallengeParseError(f"MPP-Tempo: failed to decode 'request': {exc}") from exc

        # Validate required fields
        for field in ("amount", "currency", "recipient"):
            if field not in req:
                raise ChallengeParseError(f"MPP-Tempo: 'request' missing required field '{field}'")

        try:
            amount = int(req["amount"])
        except (ValueError, TypeError) as exc:
            raise ChallengeParseError(
                f"MPP-Tempo: 'amount' must be a base-10 integer string: {exc}"
            ) from exc

        currency_contract: str = req["currency"]
        recipient: str = req["recipient"]

        method_details: dict[str, Any] = req.get("methodDetails", {})
        chain_id: int = int(method_details.get("chainId", 42431))
        fee_payer: bool = bool(method_details.get("feePayer", False))
        supported_modes: list[str] = method_details.get("supportedModes", ["pull"])

        # Validate that pull mode is supported (W13 only implements pull)
        if "pull" not in [m.lower() for m in supported_modes]:
            raise ChallengeParseError(
                f"MPP-Tempo: challenge only offers modes {supported_modes!r}; "
                "Routewiler W13 requires 'pull' mode"
            )

        # Expiry
        expires_str = params.get("expires", "")
        if expires_str:
            try:
                expires_at = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ChallengeParseError(
                    f"MPP-Tempo: could not parse 'expires' value {expires_str!r}: {exc}"
                ) from exc
        else:
            # Default: now + 5 minutes
            expires_at = datetime.fromtimestamp(time.time() + _DEFAULT_VALIDITY_SECONDS, tz=UTC)

        if datetime.now(tz=UTC) >= expires_at:
            raise ChallengeExpiredError(
                f"MPP-Tempo challenge {challenge_id!r} expired at {expires_at.isoformat()}"
            )

        # Currency string for Price
        currency_str = _tip20_currency_string(currency_contract)
        human = _human_amount(currency_contract, amount)

        # Determine network name from chain_id
        network = _CHAIN_ID_TO_NETWORK.get(chain_id, f"tempo-chain-{chain_id}")

        # Store all auth-params in raw.extra for round-tripping on the retry
        raw = MppTempoRailRaw(
            kind="mpp-tempo",
            charge_id=challenge_id,
            settlement_network="tempo",
            extra={
                "realm": params.get("realm", ""),
                "intent": params.get("intent", "charge"),
                "opaque": params.get("opaque", ""),
                "request_decoded": req,
                "auth_params": dict(params),
                "chain_id": chain_id,
                "fee_payer": fee_payer,
                "supported_modes": supported_modes,
                "network": network,
            },
        )

        return NormalizedChallenge(
            rail="mpp-tempo",
            resource=Resource(
                method=request.method,
                url=str(request.url),
                url_encoding="raw",
                original_status=402,
            ),
            price=Price(
                amount=amount,
                currency=currency_str,
                human_amount=human,
            ),
            payee=Payee(
                identifier=recipient,
                metadata={"currency_contract": currency_contract, "chain_id": chain_id},
            ),
            scheme="exact",  # MPP charge is always exact in W13
            nonce=challenge_id,  # challenge ID is the cryptographic binding
            expires_at=expires_at,
            raw=raw,
        )

    def match_funding(
        self,
        challenge: NormalizedChallenge,
        funding: Sequence[FundingSource],
    ) -> TempoFundingSource | None:
        """Return the first ``TempoFundingSource`` matching the challenge's chain + currency."""
        if not isinstance(challenge.raw, MppTempoRailRaw):
            return None

        chain_id: int = challenge.raw.extra.get("chain_id", 42431)
        currency_contract: str = challenge.payee.metadata.get("currency_contract", "")  # type: ignore[union-attr]

        for fs in funding:
            if not isinstance(fs, TempoFundingSource):
                continue
            if fs.signer.chain_id != chain_id:
                continue
            # Match by asset: either hex address or canonical name
            asset_lower = fs.asset.lower()
            contract_lower = currency_contract.lower()
            if asset_lower.startswith("0x"):
                if asset_lower == contract_lower:
                    return fs
            else:
                # Canonical name → resolve to address
                network = challenge.raw.extra.get("network", "")
                resolved = _resolve_contract(network, fs.asset)
                if resolved and resolved.lower() == contract_lower:
                    return fs
        return None

    async def pay(
        self,
        challenge: NormalizedChallenge,
        receipt: DrawReceipt | None = None,
    ) -> PaymentResult:
        """Sign a type-0x76 Tempo Transaction and build the MPP credential.

        Steps:
            1. Extract challenge fields from raw.
            2. Locate the matching ``TempoFundingSource``.
            3. Sign the transaction via ``signer.sign_transaction``.
            4. Compute the tx hash for ``proof_value``.
            5. Build the MPP credential dict and ``Authorization: Payment`` header.
            6. Return ``PaymentResult``.

        Raises:
            NoFundingForRailError:  No ``TempoFundingSource`` matches this challenge.
            MppChargeFailedError:   Signer raised an unexpected error.
        """
        assert isinstance(challenge.raw, MppTempoRailRaw), (
            "pay() called with non-MPP-Tempo challenge"
        )

        source = self.match_funding(challenge, self._funding)
        if source is None:
            raise NoFundingForRailError(
                f"No TempoFundingSource matches chain_id="
                f"{challenge.raw.extra.get('chain_id')!r}, "
                f"currency={challenge.payee.metadata.get('currency_contract')!r}. "  # type: ignore[union-attr]
                f"Available: {[(f.signer.chain_id, f.asset) for f in self._funding]}"
            )

        req: dict[str, Any] = challenge.raw.extra.get("request_decoded", {})
        currency_contract: str = req.get("currency", "")
        recipient: str = req.get("recipient", "")
        chain_id: int = challenge.raw.extra.get("chain_id", 42431)
        fee_payer: bool = challenge.raw.extra.get("fee_payer", False)

        # Compute a validity window from the challenge expiry
        valid_until = int(challenge.expires_at.timestamp())

        # Nonce: in production this must be fetched from the Tempo RPC.
        # For W13 we default to 0 (acceptable for testnet / single-use challenges).
        nonce = 0

        try:
            signed_tx_hex = await source.signer.sign_transaction(
                tip20_token=currency_contract,
                recipient=recipient,
                amount=challenge.price.amount,
                nonce_key=0,
                nonce=nonce,
                valid_until=valid_until,
                fee_payer=fee_payer,
            )
        except Exception as exc:
            raise MppChargeFailedError(
                f"MPP-Tempo signing failed for challenge {challenge.raw.charge_id!r}: {exc}"
            ) from exc

        computed_hash = tempo_tx_hash(signed_tx_hex)

        # Build the MPP credential per draft-httpauth-payment-00 / draft-tempo-charge-00
        auth_params = challenge.raw.extra.get("auth_params", {})
        credential: dict[str, Any] = {
            "challenge": {
                "id": challenge.raw.charge_id,
                "realm": auth_params.get("realm", ""),
                "method": "tempo",
                "intent": auth_params.get("intent", "charge"),
                "request": auth_params.get("request", ""),
                "expires": auth_params.get("expires", ""),
                "opaque": auth_params.get("opaque", ""),
            },
            "payload": {
                "type": "transaction",
                "signature": signed_tx_hex,
            },
            "source": f"did:pkh:eip155:{chain_id}:{source.signer.address}",
        }
        # Strip empty-string keys from challenge to keep the blob compact
        credential["challenge"] = {k: v for k, v in credential["challenge"].items() if v != ""}

        header_value = build_authorization_header(credential)

        persisted: dict[str, Any] = {
            "charge_id": challenge.raw.charge_id,
            "signed_tx": signed_tx_hex,
            "tx_hash": computed_hash,
            "chain_id": chain_id,
            "challenge_echo": credential["challenge"],
        }

        return PaymentResult(
            header_name=_AUTHORIZATION_HEADER,
            header_value=header_value,
            credential=persisted,
            proof_type=self.proof_type,  # "txid"
            proof_value=computed_hash,
        )

    async def confirm(
        self,
        result: PaymentResult,
        response: httpx.Response,
    ) -> SettlementInfo | None:
        """Parse the ``Payment-Receipt`` header and return settlement info.

        Returns:
            ``SettlementInfo`` with the on-chain reference (tx hash) and
            the settled amount.

        Raises:
            MppReceiptVerificationError: Receipt is malformed or mismatches
                                         the credential we sent.
        """
        receipt_header = response.headers.get(_PAYMENT_RECEIPT_HEADER, "")
        if not receipt_header:
            # Receipt absent — still return minimal SettlementInfo
            return SettlementInfo(
                success=response.is_success,
                tx_hash=result.proof_value,
                network_id="tempo",
                payer_address=None,
                amount_paid=None,
            )

        try:
            receipt = parse_payment_receipt(receipt_header)
        except Exception as exc:
            raise MppReceiptVerificationError(
                f"MPP-Tempo: failed to decode Payment-Receipt: {exc}"
            ) from exc

        # Cross-check challenge ID and method
        expected_id = (result.credential or {}).get("charge_id", "")
        if expected_id and receipt.challenge_id != expected_id:
            raise MppReceiptVerificationError(
                f"MPP-Tempo: receipt challengeId {receipt.challenge_id!r} != "
                f"expected {expected_id!r}"
            )
        if receipt.method != "tempo":
            raise MppReceiptVerificationError(
                f"MPP-Tempo: receipt method {receipt.method!r} != 'tempo'"
            )

        payer_address: str | None = None
        if result.credential:
            source_did = result.credential.get("challenge_echo", {}).get("source", "")
            if not source_did and result.credential:
                # Fallback: build source DID from stored chain_id
                pass
            # Extract address from persisted credential — not the DID (we don't store it there)
            # The source DID is only in the credential blob, not persisted separately; skip.

        # Settled amount in base units
        try:
            amount_paid = int(receipt.settlement.get("amount", "0"))
        except (ValueError, TypeError):
            amount_paid = None

        return SettlementInfo(
            success=receipt.status == "success" and response.is_success,
            tx_hash=receipt.reference,
            network_id="tempo",
            payer_address=payer_address,
            amount_paid=amount_paid,
        )

    async def sign(self, challenge: NormalizedChallenge) -> str:
        raise NotImplementedError(
            "MppTempoAdapter uses pay() not sign(). "
            "sign() is a legacy method for x402-style header-signing adapters."
        )

    def parse_settlement(self, response: httpx.Response) -> SettlementInfo | None:
        return None


# ---------------------------------------------------------------------------
# Verify protocol conformance at import time
# ---------------------------------------------------------------------------

assert isinstance(MppTempoAdapter([]), RailAdapter), (
    "MppTempoAdapter does not satisfy the RailAdapter protocol"
)
