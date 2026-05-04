"""x402 v2 rail adapter — detector, parser, and signer."""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
from x402 import parse_payment_required, x402Client
from x402.mechanisms.evm.exact.register import register_exact_evm_client
from x402.mechanisms.evm.signers import EthAccountSigner

from routewiler._constants import HTTP_STATUS_PAYMENT_REQUIRED
from routewiler.assets import ASSETS, ASSETS_BY_ADDRESS, CANONICAL_ADDRESSES, CHAIN_IDS
from routewiler.errors import ChallengeParseError, NoFundingForRailError, SigningError
from routewiler.funding import FundingSource
from routewiler.funding.evm import EvmFundingSource
from routewiler.normalized import (
    NormalizedChallenge,
    Payee,
    Price,
    ProofType,
    Rail,
    Resource,
    X402PaymentRequirements,
    X402RailRaw,
)
from routewiler.rails.base import PaymentResult, SettlementInfo

# Re-export SettlementInfo so existing importers of this module are unaffected.
__all__ = ["PaymentResult", "SettlementInfo", "X402Adapter"]


def _resolve_asset(network: str, asset: str) -> str:
    """Return the lowercase ERC-20 address for a given (network, asset) pair.

    If `asset` is already an address (starts with "0x"), return it as-is.
    Otherwise look up the canonical address in the asset registry; fall back
    to `asset` unchanged.
    """
    a = asset.lower()
    if a.startswith("0x"):
        return a
    return CANONICAL_ADDRESSES.get((network, a), a)


def _to_caip19(network: str, asset: str) -> str:
    """Format a CAIP-19 currency identifier for the given EVM network + asset."""
    chain_id = CHAIN_IDS.get(network)
    if chain_id is None:
        return f"{network}/{asset.lower()}"
    address = _resolve_asset(network, asset)
    return f"eip155:{chain_id}/erc20:{address}"


def _human_amount(asset: str, raw_str: str) -> str:
    """Format a human-readable amount string, e.g. "0.01 USDC"."""
    asset_lower = asset.lower()
    try:
        raw = int(raw_str)
    except (ValueError, TypeError):
        return f"{raw_str} {asset}"
    # Look up by canonical name first, then by address, then fall back to 18.
    meta = ASSETS.get(asset_lower) or ASSETS_BY_ADDRESS.get(asset_lower)
    decimals = meta.decimals if meta else 18
    symbol = meta.symbol if meta else asset[:8]
    human = raw / 10**decimals
    return f"{human:g} {symbol}"


# ---------------------------------------------------------------------------
# X402Adapter
# ---------------------------------------------------------------------------

_PAYMENT_REQUIRED_HEADER = "PAYMENT-REQUIRED"
_PAYMENT_RESPONSE_HEADER = "PAYMENT-RESPONSE"


_PAYMENT_SIGNATURE_HEADER = "PAYMENT-SIGNATURE"


class X402Adapter:
    """Rail adapter for the x402 v2 protocol.

    Detector: checks for HTTP 402 + ``PAYMENT-REQUIRED`` header.
    Parser:   base64-decodes the header, validates the ``accepts`` list.
    Signer:   delegates to the ``x402`` Python SDK (``x402Client`` +
              ``ExactEvmScheme``) and returns the ``PAYMENT-SIGNATURE`` value.
    """

    rail: Rail = "x402"
    proof_type: ProofType = "txid"

    def __init__(
        self,
        funding_sources: list[EvmFundingSource],
        *,
        _x402_client: x402Client | None = None,
    ) -> None:
        self._funding = funding_sources
        self._x402 = _x402_client or x402Client()
        for fs in funding_sources:
            register_exact_evm_client(self._x402, EthAccountSigner(fs.wallet))

    # ------------------------------------------------------------------
    # RailAdapter protocol
    # ------------------------------------------------------------------

    def can_handle(self, response: httpx.Response) -> bool:
        return (
            response.status_code == HTTP_STATUS_PAYMENT_REQUIRED
            and _PAYMENT_REQUIRED_HEADER in response.headers
        )

    def parse(self, request: httpx.Request, response: httpx.Response) -> NormalizedChallenge:
        raw_header = response.headers.get(_PAYMENT_REQUIRED_HEADER, "")
        try:
            decoded = base64.b64decode(raw_header)
            data: dict[str, Any] = json.loads(decoded)
        except Exception as exc:
            raise ChallengeParseError(f"Cannot decode PAYMENT-REQUIRED header: {exc}") from exc

        x402_version: int = int(data.get("x402Version", 1))

        # The x402 wire uses "accepts";
        if "accepts" in data:
            accepts_raw = data["accepts"]
        else:
            accepts_raw = None

        if accepts_raw is None:
            raise ChallengeParseError("PAYMENT-REQUIRED payload has no 'accepts' field")
        if isinstance(accepts_raw, dict):
            accepts_raw = [accepts_raw]
        if not accepts_raw:
            raise ChallengeParseError("PAYMENT-REQUIRED accepts list is empty")

        try:
            accepts = [X402PaymentRequirements.model_validate(pr) for pr in accepts_raw]
        except Exception as exc:
            raise ChallengeParseError(f"Invalid PaymentRequirements entry: {exc}") from exc

        chosen = self._select(accepts)  # raises NoFundingForRailError if no match
        raw = X402RailRaw(kind="x402", accepts=accepts, x402_version=x402_version)

        # Nonce and expiry live in chosen.extra for EVM schemes.
        nonce: str = chosen.extra.get("nonce") or uuid4().hex
        valid_before = chosen.extra.get("validBefore")
        if valid_before:
            expires_at = datetime.fromtimestamp(int(valid_before), tz=UTC)
        else:
            expires_at = datetime.now(UTC) + timedelta(seconds=chosen.max_timeout_seconds)

        return NormalizedChallenge(
            rail="x402",
            resource=Resource(
                method=request.method,
                url=str(request.url),
                url_encoding="raw",  # emitter overwrites this based on configured url_mode
                original_status=402,
            ),
            price=Price(
                amount=int(chosen.max_amount_required),
                currency=_to_caip19(chosen.network, chosen.asset),
                human_amount=_human_amount(chosen.asset, chosen.max_amount_required),
            ),
            payee=Payee(identifier=chosen.pay_to),
            scheme=chosen.scheme,
            nonce=nonce,
            expires_at=expires_at,
            raw=raw,
        )

    async def sign(self, challenge: NormalizedChallenge) -> str:
        assert isinstance(challenge.raw, X402RailRaw), "sign called with non-x402 challenge"
        # Reconstruct a typed PaymentRequired so the x402 SDK can select and sign.
        # Use the version captured from the original wire payload (not hardcoded 1).
        raw_dict: dict[str, Any] = {
            "x402Version": challenge.raw.x402_version,
            "accepts": [pr.model_dump(by_alias=True) for pr in challenge.raw.accepts],
        }
        payment_required = parse_payment_required(raw_dict)
        try:
            payload = await self._x402.create_payment_payload(payment_required)
        except Exception as exc:
            raise SigningError(f"x402 SDK signing failed: {exc}") from exc

        # SDK returns a Pydantic model (PaymentPayload / PaymentPayloadV1), a dict,
        # or an already-encoded string depending on version.
        if isinstance(payload, str):
            return payload
        if isinstance(payload, dict):
            serialized = json.dumps(payload, separators=(",", ":"))
        else:
            # Pydantic model — serialize via model_dump to respect field aliases.
            serialized = json.dumps(payload.model_dump(by_alias=True), separators=(",", ":"))
        return base64.b64encode(serialized.encode()).decode()

    def match_funding(
        self,
        challenge: NormalizedChallenge,
        funding: Sequence[FundingSource],
    ) -> EvmFundingSource | None:
        """Return the first funding source that can satisfy this x402 challenge."""
        if not isinstance(challenge.raw, X402RailRaw):
            return None
        for pr in challenge.raw.accepts:
            for fs in funding:
                if not isinstance(fs, EvmFundingSource):
                    continue
                if pr.network != fs.network:
                    continue
                pr_asset = _resolve_asset(pr.network, pr.asset)
                fs_asset = _resolve_asset(fs.network, fs.asset)
                if pr_asset == fs_asset:
                    return fs
        return None

    async def pay(
        self,
        challenge: NormalizedChallenge,
        receipt: Any = None,  # DrawReceipt | None — unused for x402
    ) -> PaymentResult:
        """Sign the x402 challenge and return a PaymentResult with the header."""
        header_value = await self.sign(challenge)
        return PaymentResult(
            header_name=_PAYMENT_SIGNATURE_HEADER,
            header_value=header_value,
            credential=None,
            proof_type=self.proof_type,
            proof_value=None,  # filled by confirm() from PAYMENT-RESPONSE
        )

    async def confirm(
        self,
        result: PaymentResult,
        response: httpx.Response,
    ) -> SettlementInfo | None:
        """Read PAYMENT-RESPONSE header from the server's successful reply."""
        settlement = self.parse_settlement(response)
        if settlement is not None and settlement.tx_hash is not None:
            return settlement
        return settlement

    def parse_settlement(self, response: httpx.Response) -> SettlementInfo | None:
        """Read the PAYMENT-RESPONSE header from a successful reply.

        Returns None if the header is absent or cannot be decoded — callers
        should treat a missing settlement as `proof_value=None` in the trace.
        """
        raw = response.headers.get(_PAYMENT_RESPONSE_HEADER, "")
        if not raw:
            return None
        try:
            data: dict[str, Any] = json.loads(base64.b64decode(raw))
        except Exception:
            return None
        amount_raw = data.get("amountPaid")
        amount_paid: int | None = None
        if amount_raw is not None:
            try:
                amount_paid = int(amount_raw)
            except (ValueError, TypeError):
                pass
        return SettlementInfo(
            success=bool(data.get("success", False)),
            tx_hash=data.get("txHash") or data.get("transaction") or None,
            network_id=data.get("networkId") or data.get("network") or None,
            payer_address=data.get("payerAddress") or data.get("payer") or None,
            amount_paid=amount_paid,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _select(self, accepts: list[X402PaymentRequirements]) -> X402PaymentRequirements:
        """Pick the first PaymentRequirements entry our funding can satisfy."""
        for pr in accepts:
            for fs in self._funding:
                if pr.network != fs.network:
                    continue
                # Match: canonical name OR resolved address.
                pr_asset = _resolve_asset(pr.network, pr.asset)
                fs_asset = _resolve_asset(fs.network, fs.asset)
                if pr_asset == fs_asset:
                    return pr
        raise NoFundingForRailError(
            f"No funding source matches any of the {len(accepts)} offered payment options. "
            f"Available: {[(fs.network, fs.asset) for fs in self._funding]}"
        )
