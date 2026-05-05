"""Tempo Transaction (type 0x76) encoder and signer.

Implements the ``draft-tempo-charge-00`` transaction format from paymentauth.org.
The Tempo Transaction is an EIP-2718 envelope with type prefix ``0x76`` that
carries batched TIP-20 token calls, a 2D nonce, a validity window, and a
secp256k1 signature.

Wire format (simplified single-call variant shipped in W13):
    0x76 || rlp([
        chain_id,
        nonce_key,
        nonce,
        valid_until,
        fee_token,            # TIP-20 address (or zero if fee_payer=True)
        fee_amount,           # 0 when fee_payer=True
        calls,                # list of [to, calldata] pairs
        fee_payer_signature,  # 0x00 placeholder when fee_payer=True
        sig_v,
        sig_r,
        sig_s,
    ])

The signing digest is keccak256(0x76 || rlp(unsigned_fields)) where
unsigned_fields are all elements except the three trailing signature fields.
"""

from __future__ import annotations

from typing import Any

from eth_account.signers.local import LocalAccount
from eth_hash.auto import keccak
from eth_keys.datatypes import PrivateKey as EthPrivateKey

# ---------------------------------------------------------------------------
# TIP-20 ABI encoding helpers
# ---------------------------------------------------------------------------

_TRANSFER_SELECTOR: bytes = bytes.fromhex("a9059cbb")  # transfer(address,uint256)


def _encode_transfer_calldata(recipient: str, amount: int) -> bytes:
    """Encode TIP-20 transfer(address,uint256) calldata.

    Manual ABI encoding — no ``eth_abi`` dependency for a fixed two-argument
    function signature.

    Args:
        recipient:  ``0x``-prefixed hex address (checksummed or lowercase).
        amount:     Token amount in base units (6 decimals for PathUSD/USDC).

    Returns:
        4-byte selector + 32-byte padded address + 32-byte big-endian amount.
    """
    addr_bytes = bytes.fromhex(recipient.removeprefix("0x").zfill(40))
    padded_addr = b"\x00" * 12 + addr_bytes  # left-pad address to 32 bytes
    padded_amount = amount.to_bytes(32, "big")
    return _TRANSFER_SELECTOR + padded_addr + padded_amount


# ---------------------------------------------------------------------------
# RLP encoding (minimal, sufficient for Tempo Transaction structure)
# ---------------------------------------------------------------------------


def _rlp_encode_item(value: Any) -> bytes:
    """Minimal RLP encoder for Tempo Transaction fields.

    Handles:
        bytes  → RLP string
        int    → big-endian bytes then RLP string (unsigned, min-length)
        list   → RLP list of recursively encoded items
    """
    if isinstance(value, int):
        if value == 0:
            as_bytes: bytes = b""
        else:
            byte_len = (value.bit_length() + 7) // 8
            as_bytes = value.to_bytes(byte_len, "big")
        return _rlp_encode_item(as_bytes)

    if isinstance(value, bytes):
        if len(value) == 1 and value[0] < 0x80:
            return value  # single byte [0x00, 0x7f]: self-describing
        return _rlp_length_prefix(len(value), 0x80) + value

    if isinstance(value, list):
        payload = b"".join(_rlp_encode_item(item) for item in value)
        return _rlp_length_prefix(len(payload), 0xC0) + payload

    raise TypeError(f"RLP: unsupported type {type(value)!r}")


def _rlp_length_prefix(length: int, offset: int) -> bytes:
    if length < 56:
        return bytes([offset + length])
    length_bytes = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([offset + 55 + len(length_bytes)]) + length_bytes


# ---------------------------------------------------------------------------
# Address / hex helpers
# ---------------------------------------------------------------------------


def _addr_bytes(hex_address: str) -> bytes:
    """20-byte address from a hex string (``0x``-prefixed or bare)."""
    return bytes.fromhex(hex_address.removeprefix("0x").zfill(40))


def _zero_address() -> bytes:
    return b"\x00" * 20


# ---------------------------------------------------------------------------
# Tempo Transaction builder & signer
# ---------------------------------------------------------------------------


def sign_tempo_transaction(
    *,
    wallet: LocalAccount,
    chain_id: int,
    tip20_token: str,
    recipient: str,
    amount: int,
    nonce_key: int = 0,
    nonce: int,
    valid_until: int,
    fee_payer: bool = False,
    memo: bytes = b"\x00" * 32,
) -> str:
    """Build and sign a Tempo Transaction (type 0x76) for a single TIP-20 transfer.

    Implements pull-mode signing per ``draft-tempo-charge-00``:
    the signed transaction is returned for inclusion in the MPP credential;
    the server broadcasts it to the Tempo RPC.

    Args:
        wallet:       eth_account LocalAccount holding the secp256k1 private key.
        chain_id:     Tempo chain ID (42431 = Moderato, 42430 = mainnet).
        tip20_token:  Contract address of the TIP-20 token to transfer.
        recipient:    Recipient address.
        amount:       Transfer amount in base units (6 decimals).
        nonce_key:    2D nonce lane (0 = standard payment lane).
        nonce:        Per-lane sequence number.
        valid_until:  Unix timestamp (seconds) after which the tx is rejected.
        fee_payer:    If True the server sponsors fees (placeholder sig).
        memo:         32-byte memo (unused in W13 single-transfer variant).

    Returns:
        ``0x76``-prefixed, RLP-encoded signed transaction as a ``0x``-prefixed
        hex string (e.g. ``0x76...``).
    """
    calldata = _encode_transfer_calldata(recipient, amount)
    token_bytes = _addr_bytes(tip20_token)

    if fee_payer:
        fee_token: bytes = _zero_address()
        fee_amount = 0
        fee_payer_sig: bytes = b"\x00"  # single zero byte = placeholder
    else:
        fee_token = token_bytes
        fee_amount = 0  # W13: fee mechanics deferred; server must tolerate 0
        fee_payer_sig = b""  # empty = not applicable

    # calls: list of [to, calldata]
    calls: list[Any] = [[token_bytes, calldata]]

    unsigned_body: list[Any] = [
        chain_id,
        nonce_key,
        nonce,
        valid_until,
        fee_token,
        fee_amount,
        calls,
        fee_payer_sig,
    ]

    # Signing digest: keccak256(type_prefix || rlp(unsigned_body))
    type_prefix = b"\x76"
    rlp_unsigned = _rlp_encode_item(unsigned_body)
    digest: bytes = keccak(type_prefix + rlp_unsigned)

    # Sign using eth-keys secp256k1 (no Ethereum prefix — raw hash signing)
    pk = EthPrivateKey(bytes(wallet.key))
    sig = pk.sign_msg_hash(digest)

    # sig.v is 0 or 1 (recovery id); Tempo uses 27/28 Ethereum convention
    v = sig.v + 27
    r = sig.r.to_bytes(32, "big")
    s = sig.s.to_bytes(32, "big")

    signed_body: list[Any] = list(unsigned_body) + [v, r, s]
    signed_rlp = _rlp_encode_item(signed_body)

    return "0x76" + signed_rlp.hex()


def tx_hash(signed_tx_hex: str) -> str:
    """Compute the Tempo transaction hash (keccak256 of the raw signed bytes).

    This is the ``reference`` field in the ``Payment-Receipt`` header.

    Args:
        signed_tx_hex:  ``0x76``-prefixed hex string from ``sign_tempo_transaction``.

    Returns:
        ``0x``-prefixed 32-byte hex hash.
    """
    raw = bytes.fromhex(signed_tx_hex.removeprefix("0x"))
    digest: bytes = keccak(raw)
    return "0x" + digest.hex()
