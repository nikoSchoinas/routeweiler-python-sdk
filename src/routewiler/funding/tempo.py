"""Tempo funding source — wraps an eth_account signer for MPP-Tempo payments.

The Tempo blockchain (chain ID 42431 on Moderato testnet) is EVM-compatible
at the key level: accounts are secp256k1 keypairs, addresses are 20-byte
Ethereum-style checksummed hex.  However, Tempo uses a custom EIP-2718
type-0x76 transaction format (``draft-tempo-charge-00``) rather than standard
EIP-1559 or EIP-3009 transfers.  The ``TempoSigner`` Protocol abstracts this
signing primitive so the adapter can be tested without a live chain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from eth_account.signers.local import LocalAccount

from routewiler.assets import CHAIN_IDS


@runtime_checkable
class TempoSigner(Protocol):
    """Minimum interface any Tempo key material must implement.

    Concrete implementations:
        - ``EthAccountTempoSigner`` — backed by ``eth_account.LocalAccount``.
        - ``FakeTempoSigner``       — deterministic synthetic signer for tests.
    """

    @property
    def chain_id(self) -> int:
        """Tempo chain ID (42431 = Moderato testnet, 42430 = mainnet)."""
        ...

    @property
    def address(self) -> str:
        """0x-prefixed EIP-55 checksummed Ethereum address."""
        ...

    async def sign_transaction(
        self,
        *,
        tip20_token: str,
        recipient: str,
        amount: int,
        nonce_key: int = 0,
        nonce: int,
        valid_until: int,
        fee_payer: bool = False,
        memo: bytes = b"\x00" * 32,
    ) -> str:
        """Build and sign a type-0x76 Tempo Transaction.

        Returns the complete RLP-encoded signed transaction as a ``0x``-prefixed
        hex string, ready for ``Authorization: Payment`` credential embedding or
        direct broadcast via ``eth_sendRawTransaction``.

        Args:
            tip20_token:  Hex address of the TIP-20 token contract.
            recipient:    Hex address of the payment recipient.
            amount:       Token amount in base units (6 decimals for PathUSD/USDC).
            nonce_key:    2D nonce lane (0 is the standard payment lane).
            nonce:        Per-lane sequence number to prevent replay.
            valid_until:  Unix timestamp after which the transaction is invalid.
            fee_payer:    If True, fee is sponsored by the server (placeholder sig).
            memo:         Optional 32-byte memo field (zeroes = no memo).
        """
        ...

    async def sign_typed_data_v4(self, typed_data: dict[str, Any]) -> str:
        """Sign EIP-712 typed data (used for zero-amount proof credentials).

        Returns the 65-byte signature as a ``0x``-prefixed hex string.
        """
        ...


@dataclass(frozen=True)
class TempoFundingSource:
    """A Tempo signer plus the network and asset it operates on.

    ``signer`` must satisfy the ``TempoSigner`` protocol.  Pass an
    ``EthAccountTempoSigner`` for real usage, or a ``FakeTempoSigner`` in tests.

    ``network`` must match the Tempo network identifier:
        ``"tempo-moderato"`` → chain ID 42431 (testnet)
        ``"tempo"``          → chain ID 42430 (mainnet, reserved)

    ``asset`` is the canonical short name of the TIP-20 token:
        ``"pathusd"`` → Moderato testnet (faucet-funded)
        ``"usdc"``    → Tempo mainnet
    """

    signer: TempoSigner
    network: Literal["tempo", "tempo-moderato"]
    asset: str  # "pathusd" | "usdc" | explicit hex contract address


# ---------------------------------------------------------------------------
# EthAccountTempoSigner — backed by eth_account.LocalAccount
# ---------------------------------------------------------------------------


class EthAccountTempoSigner:
    """TempoSigner backed by an eth_account ``LocalAccount``.

    Uses the ``_tempo_tx`` internal module to build and sign the type-0x76
    Tempo Transaction envelope.  The ``LocalAccount``'s secp256k1 private key
    is used for signing; it never leaves the process.
    """

    def __init__(self, *, wallet: LocalAccount, chain_id: int) -> None:
        self._wallet = wallet
        self._chain_id = chain_id

    @property
    def chain_id(self) -> int:
        return self._chain_id

    @property
    def address(self) -> str:
        """Return the EIP-55 checksummed address."""
        return str(self._wallet.address)

    async def sign_transaction(
        self,
        *,
        tip20_token: str,
        recipient: str,
        amount: int,
        nonce_key: int = 0,
        nonce: int,
        valid_until: int,
        fee_payer: bool = False,
        memo: bytes = b"\x00" * 32,
    ) -> str:
        from routewiler.rails._tempo_tx import sign_tempo_transaction  # noqa: PLC0415

        return sign_tempo_transaction(
            wallet=self._wallet,
            chain_id=self._chain_id,
            tip20_token=tip20_token,
            recipient=recipient,
            amount=amount,
            nonce_key=nonce_key,
            nonce=nonce,
            valid_until=valid_until,
            fee_payer=fee_payer,
            memo=memo,
        )

    async def sign_typed_data_v4(self, typed_data: dict[str, Any]) -> str:
        from eth_account import Account  # noqa: PLC0415
        from eth_account.messages import encode_typed_data  # noqa: PLC0415

        signable = encode_typed_data(full_message=typed_data)
        signed = Account.sign_message(signable, private_key=self._wallet.key)
        sig: str = "0x" + signed.signature.hex()
        return sig


def _network_to_chain_id(network: Literal["tempo", "tempo-moderato"]) -> int:
    """Resolve a Tempo network name to its chain ID."""
    return CHAIN_IDS[network]
