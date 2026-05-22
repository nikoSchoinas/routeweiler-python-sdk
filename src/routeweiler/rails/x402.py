"""x402 v2 rail adapter — detector, parser, and signer."""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
from x402 import parse_payment_required, x402Client
from x402.mechanisms.evm.exact.register import register_exact_evm_client
from x402.mechanisms.evm.signers import EthAccountSigner

from routeweiler._constants import HTTP_STATUS_PAYMENT_REQUIRED
from routeweiler.assets import CANONICAL_ADDRESSES, CHAIN_IDS
from routeweiler.assets import human_amount as _human_amount_asset
from routeweiler.errors import (
    ChallengeExpiredError,
    ChallengeParseError,
    NoFundingForRailError,
    SigningError,
)
from routeweiler.funding import FundingSource
from routeweiler.funding.evm import EvmFundingSource
from routeweiler.normalized import (
    NormalizedChallenge,
    Payee,
    Price,
    ProofType,
    Rail,
    X402PaymentRequirements,
    X402RailRaw,
)
from routeweiler.rails.base import PaymentResult, SettlementInfo, resource_from_request

# Reverse of CHAIN_IDS: chain_id int → short network name used by EvmFundingSource.
_CAIP2_TO_SHORT: dict[int, str] = {v: k for k, v in CHAIN_IDS.items()}
_X402_WIRE_VERSION = 2


def _canonical_network(network: str) -> str:
    """Normalise a network identifier to the short form used by EvmFundingSource.

    x402 v2 servers emit CAIP-2 identifiers (``"eip155:84532"``); EvmFundingSource
    uses short names (``"base-sepolia"``).  This converts between them.
    """
    if network.startswith("eip155:"):
        try:
            chain_id = int(network.split(":", 1)[1])
            return _CAIP2_TO_SHORT.get(chain_id, network)
        except (ValueError, IndexError):
            return network
    return network


def _resolve_asset(network: str, asset: str) -> str:
    """Return the lowercase ERC-20 address for a given (network, asset) pair.

    If `asset` is already an address (starts with "0x"), return it as-is.
    Otherwise look up the canonical address in the asset registry; fall back
    to `asset` unchanged.
    """
    a = asset.lower()
    if a.startswith("0x"):
        return a
    return CANONICAL_ADDRESSES.get((_canonical_network(network), a), a)


def _to_caip19(network: str, asset: str) -> str:
    """Format a CAIP-19 currency identifier for the given EVM network + asset.

    Accepts both legacy short names (``"base-sepolia"``) and CAIP-2 identifiers
    (``"eip155:84532"``) which x402 v2 servers emit directly.
    """
    address = _resolve_asset(network, asset)
    if network.startswith("eip155:"):
        return f"{network}/erc20:{address}"
    chain_id = CHAIN_IDS.get(network)
    if chain_id is None:
        return f"{network}/{address}"
    return f"eip155:{chain_id}/erc20:{address}"


def _find_match(
    accepts: list[X402PaymentRequirements],
    funding: Sequence[FundingSource],
) -> EvmFundingSource | None:
    """Return the first EvmFundingSource that matches any entry in ``accepts``, or None."""
    for pr in accepts:
        pr_network = _canonical_network(pr.network)
        for fs in funding:
            if not isinstance(fs, EvmFundingSource):
                continue
            if pr_network != _canonical_network(fs.network):
                continue
            if _resolve_asset(pr_network, pr.asset) == _resolve_asset(fs.network, fs.asset):
                return fs
    return None


_log = logging.getLogger(__name__)

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

    def __init__(self, funding_sources: list[EvmFundingSource]) -> None:
        self._funding = funding_sources
        self._x402 = x402Client()
        for fs in funding_sources:
            register_exact_evm_client(self._x402, EthAccountSigner(fs.wallet))

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
            _log.warning("x402: cannot decode PAYMENT-REQUIRED header: %s", exc)
            raise ChallengeParseError(f"Cannot decode PAYMENT-REQUIRED header: {exc}") from exc

        x402_version = data.get("x402Version")
        if x402_version != _X402_WIRE_VERSION:
            raise ChallengeParseError(
                f"Unsupported x402 protocol version: {x402_version!r}. "
                "Routeweiler supports x402 v2 only."
            )

        accepts_raw = data.get("accepts")
        if accepts_raw is None:
            raise ChallengeParseError("PAYMENT-REQUIRED payload has no 'accepts' field")
        if not isinstance(accepts_raw, list):
            raise ChallengeParseError(
                f"PAYMENT-REQUIRED 'accepts' must be a JSON array, got {type(accepts_raw).__name__}"
            )
        if not accepts_raw:
            raise ChallengeParseError("PAYMENT-REQUIRED accepts list is empty")

        try:
            accepts = [X402PaymentRequirements.model_validate(pr) for pr in accepts_raw]
        except Exception as exc:
            raise ChallengeParseError(f"Invalid PaymentRequirements entry: {exc}") from exc

        # Filter to "exact" scheme before funding-match: gives a clear "scheme not supported"
        # error for upto/stream entries instead of a misleading NoFundingForRailError.
        exact_accepts = [pr for pr in accepts if pr.scheme == "exact"]
        if not exact_accepts:
            offered = sorted({pr.scheme for pr in accepts})
            raise ChallengeParseError(
                f"x402 accepts list has no 'exact' scheme entry (offered: {offered}); "
                "only 'exact' is production-ready"
            )

        # Pick the first exact entry to populate challenge fields.  Funding availability
        # is checked in pay() and match_funding() — not here — so parse() is consistent
        # with L402 and MPP adapters which also do not raise NoFundingForRailError from parse().
        chosen = exact_accepts[0]
        raw = X402RailRaw(kind="x402", accepts=accepts, x402_version=x402_version)

        # validBefore / nonce may be pre-populated in chosen.extra by the server;
        # the client SDK generates a nonce when absent.
        nonce: str = chosen.extra.get("nonce", "")
        valid_before = chosen.extra.get("validBefore")
        if valid_before:
            expires_at = datetime.fromtimestamp(int(valid_before), tz=UTC)
        else:
            expires_at = datetime.now(UTC) + timedelta(seconds=chosen.max_timeout_seconds)

        if expires_at <= datetime.now(UTC):
            raise ChallengeExpiredError(
                f"x402 challenge already expired at {expires_at.isoformat()}"
            )

        return NormalizedChallenge(
            rail="x402",
            resource=resource_from_request(request),
            price=Price(
                amount=int(chosen.amount),
                currency=_to_caip19(chosen.network, chosen.asset),
                human_amount=_human_amount_asset(chosen.asset, chosen.amount),
            ),
            payee=Payee(identifier=chosen.pay_to),
            scheme="exact",
            nonce=nonce,
            expires_at=expires_at,
            raw=raw,
        )

    async def _sign(self, challenge: NormalizedChallenge) -> str:
        x402_raw = cast(X402RailRaw, challenge.raw)
        raw_dict: dict[str, Any] = {
            "x402Version": x402_raw.x402_version,
            "accepts": [pr.model_dump(by_alias=True) for pr in x402_raw.accepts],
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
        exact_accepts = [pr for pr in challenge.raw.accepts if pr.scheme == "exact"]
        return _find_match(exact_accepts, funding)

    async def pay(
        self,
        challenge: NormalizedChallenge,
    ) -> PaymentResult:
        """Sign the x402 challenge and return a PaymentResult with the header.

        Raises:
            NoFundingForRailError: No funding source matches this challenge.
            SigningError:          x402 SDK signing failed.
        """
        _log.debug(
            "pay: rail=%s nonce=%s amount=%s", self.rail, challenge.nonce, challenge.price.amount
        )

        x402_raw = cast(X402RailRaw, challenge.raw)
        exact_accepts = [pr for pr in x402_raw.accepts if pr.scheme == "exact"]
        if _find_match(exact_accepts, self._funding) is None:
            raise NoFundingForRailError(
                f"No funding source matches any of the {len(exact_accepts)} offered exact "
                f"payment options. Available: {[(fs.network, fs.asset) for fs in self._funding]}"
            )

        header_value = await self._sign(challenge)
        return PaymentResult(
            header_name=_PAYMENT_SIGNATURE_HEADER,
            header_value=header_value,
            credential=None,
            proof_type=self.proof_type,
            proof_value=None,  # emitter falls back to settlement.tx_hash from PAYMENT-RESPONSE
        )

    async def confirm(
        self,
        result: PaymentResult,
        response: httpx.Response,
    ) -> SettlementInfo:
        """Read PAYMENT-RESPONSE header from the server's successful reply."""
        _log.debug("confirm: status=%d", response.status_code)
        return self._parse_settlement(response)

    def _parse_settlement(self, response: httpx.Response) -> SettlementInfo:
        """Read the PAYMENT-RESPONSE header from a successful reply.

        Returns a SettlementInfo with tx_hash=None if the header is absent or
        cannot be decoded; success is derived from the HTTP status code.
        """
        raw = response.headers.get(_PAYMENT_RESPONSE_HEADER, "")
        if not raw:
            return SettlementInfo(success=response.is_success)
        try:
            data: dict[str, Any] = json.loads(base64.b64decode(raw))
        except (ValueError, TypeError):
            return SettlementInfo(success=response.is_success)
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
            facilitator=data.get("facilitator") or None,
        )
