"""Fake Tempo signer for tests — deterministic synthetic signature, no chain required."""

from __future__ import annotations

# Deterministic fake signed transaction — valid hex, not a real Tempo Transaction
FAKE_SIGNED_TX = "0x76" + "aa" * 100

# keccak256("fake") but we just use a fixed 32-byte hex for tests
FAKE_TX_HASH = "0x" + "bb" * 32


class FakeTempoSigner:
    """Minimal TempoSigner that returns a deterministic synthetic signed transaction.

    Mirrors FakeLndClient: offline, no crypto, deterministic outputs.
    """

    def __init__(
        self,
        *,
        address: str = "0xDeaDBeef" + "00" * 16,
        chain_id: int = 42431,
        signed_tx: str = FAKE_SIGNED_TX,
        should_fail: bool = False,
    ) -> None:
        self._address = address
        self._chain_id = chain_id
        self._signed_tx = signed_tx
        self._should_fail = should_fail

    @property
    def chain_id(self) -> int:
        return self._chain_id

    @property
    def address(self) -> str:
        return self._address

    async def sign_transaction(
        self,
        *,
        tip20_token: str,
        recipient: str,
        amount: int,
        nonce_key: int = 0,
        nonce: int,
        valid_before: int,
        valid_after: int = 0,
        fee_payer: bool = False,
        max_priority_fee_per_gas: int = 0,
        max_fee_per_gas: int = 20_000_000_000,
        gas_limit: int = 350_000,
    ) -> str:
        if self._should_fail:
            raise RuntimeError("FakeTempoSigner: forced failure")
        return self._signed_tx
