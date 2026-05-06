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

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import httpx

from routewiler._constants import HTTP_STATUS_PAYMENT_REQUIRED
from routewiler.assets import ASSETS_BY_ADDRESS, CANONICAL_ADDRESSES, CHAIN_IDS
from routewiler.errors import (
    ChallengeParseError,
    MppChargeFailedError,
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
    AUTHORIZATION,
    WWW_AUTHENTICATE,
    build_mpp_credential,
    compute_mpp_expiry,
    confirm_mpp_receipt,
    is_mpp_payment_for,
    parse_mpp_envelope,
    parse_required_request_fields,
)
from routewiler.rails._tempo_tx import tx_hash as tempo_tx_hash
from routewiler.rails.base import PaymentResult, SettlementInfo

if TYPE_CHECKING:
    from routewiler.budgets.schema import DrawReceipt
    from routewiler.funding import FundingSource

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


async def _fetch_nonce(rpc_url: str, address: str) -> int:
    """Fetch the pending transaction count for ``address`` via ``eth_getTransactionCount``.

    Returns 0 if ``rpc_url`` is empty (offline / test mode).

    Raises:
        MppChargeFailedError: HTTP or JSON-RPC error from the Tempo RPC endpoint.
    """
    if not rpc_url:
        return 0
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_getTransactionCount",
        "params": [address, "pending"],
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(rpc_url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        if "error" in data:
            raise MppChargeFailedError(
                f"eth_getTransactionCount RPC error from {rpc_url}: {data['error']}"
            )
        return int(data["result"], 16)
    except MppChargeFailedError:
        raise
    except Exception as exc:
        raise MppChargeFailedError(f"Failed to fetch on-chain nonce from {rpc_url}: {exc}") from exc


_log = logging.getLogger(__name__)

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
        return is_mpp_payment_for(response, {"tempo"})

    def parse(self, request: httpx.Request, response: httpx.Response) -> NormalizedChallenge:
        """Decode the MPP-Tempo 402 challenge into a ``NormalizedChallenge``.

        Raises:
            ChallengeParseError:   Malformed header, missing required fields.
            ChallengeExpiredError: Challenge ``expires`` is in the past.
        """
        header = response.headers.get(WWW_AUTHENTICATE, "")
        challenge_id, req, params = parse_mpp_envelope(header, rail_prefix="MPP-Tempo")

        parse_required_request_fields(
            req, fields=("amount", "currency", "recipient"), rail_label="MPP-Tempo"
        )

        try:
            amount = int(req["amount"])
        except (ValueError, TypeError) as exc:
            raise ChallengeParseError(
                f"MPP-Tempo: 'amount' must be a base-10 integer string: {exc}"
            ) from exc

        currency_contract: str = req["currency"]
        recipient: str = req["recipient"]

        method_details: dict[str, Any] = req.get("methodDetails", {})
        raw_chain_id = method_details.get("chainId")
        if raw_chain_id is None:
            raise ChallengeParseError(
                "MPP-Tempo challenge is missing required 'methodDetails.chainId'"
            )
        chain_id: int = int(raw_chain_id)
        fee_payer: bool = bool(method_details.get("feePayer", False))
        supported_modes: list[str] = method_details.get("supportedModes", ["pull"])

        # Validate that pull mode is supported (W13 only implements pull)
        if "pull" not in [m.lower() for m in supported_modes]:
            raise ChallengeParseError(
                f"MPP-Tempo: challenge only offers modes {supported_modes!r}; "
                "Routewiler W13 requires 'pull' mode"
            )

        expires_at = compute_mpp_expiry(params, challenge_id, rail_prefix="MPP-Tempo")

        # Currency string for Price
        currency_str = _tip20_currency_string(currency_contract)
        human = _human_amount(currency_contract, amount)

        # Determine network name from chain_id
        network = _CHAIN_ID_TO_NETWORK.get(chain_id, f"tempo-chain-{chain_id}")

        # Store all auth-params in raw.extra for round-tripping on the retry
        raw = MppTempoRailRaw(
            kind="mpp-tempo",
            charge_id=challenge_id,
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
                original_status=HTTP_STATUS_PAYMENT_REQUIRED,
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

        chain_id: int = int(challenge.raw.extra["chain_id"])
        currency_contract: str = challenge.payee.metadata.get("currency_contract", "")

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
        _log.debug(
            "pay: rail=%s nonce=%s amount=%s", self.rail, challenge.nonce, challenge.price.amount
        )

        source = self.match_funding(challenge, self._funding)
        if source is None:
            raise NoFundingForRailError(
                f"No TempoFundingSource matches chain_id="
                f"{challenge.raw.extra.get('chain_id')!r}, "
                f"currency={challenge.payee.metadata.get('currency_contract')!r}. "
                f"Available: {[(f.signer.chain_id, f.asset) for f in self._funding]}"
            )

        req: dict[str, Any] = challenge.raw.extra.get("request_decoded", {})
        currency_contract: str = req.get("currency", "")
        recipient: str = req.get("recipient", "")
        chain_id: int = int(challenge.raw.extra["chain_id"])
        fee_payer: bool = bool(challenge.raw.extra.get("fee_payer", False))

        # Compute a validity window from the challenge expiry
        valid_before = int(challenge.expires_at.timestamp())

        # Fetch the current on-chain nonce. Falls back to 0 when rpc_url is empty
        # (offline / unit-test mode where FakeTempoSigner is used).
        nonce = await _fetch_nonce(source.rpc_url, source.signer.address)

        try:
            signed_tx_hex = await source.signer.sign_transaction(
                tip20_token=currency_contract,
                recipient=recipient,
                amount=challenge.price.amount,
                nonce_key=0,
                nonce=nonce,
                valid_before=valid_before,
                fee_payer=fee_payer,
            )
        except Exception as exc:
            raise MppChargeFailedError(
                f"MPP-Tempo signing failed for challenge {challenge.raw.charge_id!r}: {exc}"
            ) from exc

        computed_hash = tempo_tx_hash(signed_tx_hex)

        # Build the MPP credential per draft-httpauth-payment-00 / draft-tempo-charge-00
        auth_params = challenge.raw.extra.get("auth_params", {})
        credential, header_value = build_mpp_credential(
            challenge_id=challenge.raw.charge_id,
            auth_params=auth_params,
            default_method="tempo",
            payload={"type": "transaction", "signature": signed_tx_hex},
            source=f"did:pkh:eip155:{chain_id}:{source.signer.address}",
        )

        persisted: dict[str, Any] = {
            "charge_id": challenge.raw.charge_id,
            "signed_tx": signed_tx_hex,
            "tx_hash": computed_hash,
            "chain_id": chain_id,
            "challenge_echo": credential["challenge"],
        }

        return PaymentResult(
            header_name=AUTHORIZATION,
            header_value=header_value,
            credential=persisted,
            proof_type=self.proof_type,  # "txid"
            proof_value=computed_hash,
        )

    async def confirm(
        self,
        result: PaymentResult,
        response: httpx.Response,
    ) -> SettlementInfo:
        """Parse the ``Payment-Receipt`` header and return settlement info.

        Returns:
            ``SettlementInfo`` with the on-chain reference (tx hash) and
            the settled amount.

        Raises:
            MppReceiptVerificationError: Receipt is malformed or mismatches
                                         the credential we sent.
        """
        _log.debug("confirm: status=%d", response.status_code)
        return confirm_mpp_receipt(
            result,
            response,
            expected_methods={"tempo"},
            network_id="tempo",
            rail_prefix="MPP-Tempo",
            facilitator="tempo",
        )
