"""Unit tests for _tempo_tx.py — Tempo Transaction encoder and signer."""

from __future__ import annotations

from eth_account import Account
from eth_hash.auto import keccak
from eth_keys.datatypes import PrivateKey as EthPrivateKey

from routewiler.rails._tempo_tx import (
    _addr_bytes,
    _encode_transfer_calldata,
    _rlp_encode_item,
    sign_tempo_transaction,
    tx_hash,
)

# ---------------------------------------------------------------------------
# Reference constants
# ---------------------------------------------------------------------------

# Deterministic test wallet — DO NOT USE WITH REAL FUNDS.
_TEST_PRIVATE_KEY = "0x" + "aa" * 32
_TEST_WALLET = Account.from_key(_TEST_PRIVATE_KEY)
_TEST_ADDRESS = _TEST_WALLET.address  # EIP-55 checksummed

_TEST_RECIPIENT = "0x" + "bb" * 20
_TEST_TOKEN = "0x" + "cc" * 20
_TEST_AMOUNT = 10_000  # 0.01 PathUSD in base units (6 decimals)
_TEST_CHAIN_ID = 42431  # Tempo Moderato testnet

# transfer(address,uint256) selector: keccak256("transfer(address,uint256)")[:4]
_TRANSFER_SELECTOR_HEX = "a9059cbb"


# ---------------------------------------------------------------------------
# TIP-20 calldata encoding
# ---------------------------------------------------------------------------


def test_encode_transfer_calldata_selector() -> None:
    data = _encode_transfer_calldata(_TEST_RECIPIENT, _TEST_AMOUNT)
    assert data[:4].hex() == _TRANSFER_SELECTOR_HEX


def test_encode_transfer_calldata_length() -> None:
    data = _encode_transfer_calldata(_TEST_RECIPIENT, _TEST_AMOUNT)
    # 4 (selector) + 32 (addr) + 32 (amount) = 68 bytes
    assert len(data) == 68


def test_encode_transfer_calldata_address_padded() -> None:
    data = _encode_transfer_calldata(_TEST_RECIPIENT, _TEST_AMOUNT)
    # bytes 4-35: 12 zero bytes then the 20-byte address
    addr_field = data[4:36]
    assert addr_field[:12] == b"\x00" * 12
    assert addr_field[12:] == bytes.fromhex(_TEST_RECIPIENT.removeprefix("0x"))


def test_encode_transfer_calldata_amount_big_endian() -> None:
    data = _encode_transfer_calldata(_TEST_RECIPIENT, _TEST_AMOUNT)
    amount_field = data[36:68]
    assert int.from_bytes(amount_field, "big") == _TEST_AMOUNT


def test_encode_transfer_calldata_prefixed_or_bare() -> None:
    with_prefix = _encode_transfer_calldata("0x" + "dd" * 20, 1)
    bare = _encode_transfer_calldata("dd" * 20, 1)
    assert with_prefix == bare


# ---------------------------------------------------------------------------
# RLP encoding
# ---------------------------------------------------------------------------


def test_rlp_encode_zero_int() -> None:
    encoded = _rlp_encode_item(0)
    # zero int → empty bytes → RLP string length 0 → single byte 0x80
    assert encoded == b"\x80"


def test_rlp_encode_small_int() -> None:
    encoded = _rlp_encode_item(1)
    # 1 as bytes is b'\x01', which is a single byte < 0x80 → self-describing
    assert encoded == b"\x01"


def test_rlp_encode_empty_bytes() -> None:
    assert _rlp_encode_item(b"") == b"\x80"


def test_rlp_encode_list() -> None:
    result = _rlp_encode_item([1, 2, 3])
    # Each of 1, 2, 3 encodes to 1 byte; list payload = 3 bytes; list header = 0xC0 + 3
    assert result[0] == 0xC0 + 3
    assert len(result) == 4


# ---------------------------------------------------------------------------
# sign_tempo_transaction
# ---------------------------------------------------------------------------


def test_sign_tempo_transaction_prefix() -> None:
    tx = sign_tempo_transaction(
        wallet=_TEST_WALLET,
        chain_id=_TEST_CHAIN_ID,
        tip20_token=_TEST_TOKEN,
        recipient=_TEST_RECIPIENT,
        amount=_TEST_AMOUNT,
        nonce=0,
        valid_before=9_999_999_999,
    )
    assert tx.startswith("0x76"), f"Expected 0x76 prefix, got: {tx[:6]}"


def test_sign_tempo_transaction_returns_hex() -> None:
    tx = sign_tempo_transaction(
        wallet=_TEST_WALLET,
        chain_id=_TEST_CHAIN_ID,
        tip20_token=_TEST_TOKEN,
        recipient=_TEST_RECIPIENT,
        amount=_TEST_AMOUNT,
        nonce=0,
        valid_before=9_999_999_999,
    )
    # Should parse as valid hex (after stripping 0x)
    bytes.fromhex(tx.removeprefix("0x"))


def test_sign_tempo_transaction_signature_recovers_signer() -> None:
    """The signer address recoverable from the embedded secp256k1 signature must match."""
    sign_tempo_transaction(
        wallet=_TEST_WALLET,
        chain_id=_TEST_CHAIN_ID,
        tip20_token=_TEST_TOKEN,
        recipient=_TEST_RECIPIENT,
        amount=_TEST_AMOUNT,
        nonce=0,
        valid_before=9_999_999_999,
        fee_payer=False,
    )

    # Re-compute the signing digest using the new field order
    calldata = _encode_transfer_calldata(_TEST_RECIPIENT, _TEST_AMOUNT)
    token_bytes = _addr_bytes(_TEST_TOKEN)
    calls = [[token_bytes, 0, calldata]]
    unsigned_body = [
        _TEST_CHAIN_ID,    # chain_id
        0,                 # max_priority_fee_per_gas
        20_000_000_000,    # max_fee_per_gas
        200_000,           # gas_limit
        calls,             # calls [[to, 0, calldata]]
        [],                # access_list
        0,                 # nonce_key
        0,                 # nonce
        9_999_999_999,     # valid_before
        0,                 # valid_after
        token_bytes,       # fee_token
        b"",               # fee_payer_signature (empty = not sponsoring)
        [],                # aa_authorization_list
    ]
    type_prefix = b"\x76"
    digest = keccak(type_prefix + _rlp_encode_item(unsigned_body))

    # The private key that signed it
    pk = EthPrivateKey(bytes(_TEST_WALLET.key))
    sig = pk.sign_msg_hash(digest)
    recovered_addr = sig.recover_public_key_from_msg_hash(digest).to_checksum_address()
    assert recovered_addr == _TEST_ADDRESS


def test_sign_tempo_transaction_fee_payer_mode() -> None:
    tx = sign_tempo_transaction(
        wallet=_TEST_WALLET,
        chain_id=_TEST_CHAIN_ID,
        tip20_token=_TEST_TOKEN,
        recipient=_TEST_RECIPIENT,
        amount=_TEST_AMOUNT,
        nonce=0,
        valid_before=9_999_999_999,
        fee_payer=True,
    )
    assert tx.startswith("0x76")


def test_sign_tempo_transaction_deterministic() -> None:
    kwargs = dict(
        wallet=_TEST_WALLET,
        chain_id=_TEST_CHAIN_ID,
        tip20_token=_TEST_TOKEN,
        recipient=_TEST_RECIPIENT,
        amount=_TEST_AMOUNT,
        nonce=0,
        valid_before=9_999_999_999,
    )
    tx1 = sign_tempo_transaction(**kwargs)  # type: ignore[arg-type]
    tx2 = sign_tempo_transaction(**kwargs)  # type: ignore[arg-type]
    assert tx1 == tx2


# ---------------------------------------------------------------------------
# tx_hash
# ---------------------------------------------------------------------------


def test_tx_hash_format() -> None:
    tx = sign_tempo_transaction(
        wallet=_TEST_WALLET,
        chain_id=_TEST_CHAIN_ID,
        tip20_token=_TEST_TOKEN,
        recipient=_TEST_RECIPIENT,
        amount=_TEST_AMOUNT,
        nonce=0,
        valid_before=9_999_999_999,
    )
    h = tx_hash(tx)
    assert h.startswith("0x")
    assert len(h) == 66  # 0x + 64 hex chars = 32 bytes


def test_tx_hash_different_txs_differ() -> None:
    kwargs_base = dict(
        wallet=_TEST_WALLET,
        chain_id=_TEST_CHAIN_ID,
        tip20_token=_TEST_TOKEN,
        recipient=_TEST_RECIPIENT,
        amount=_TEST_AMOUNT,
        nonce=0,
        valid_before=9_999_999_999,
    )
    tx1 = sign_tempo_transaction(**kwargs_base)  # type: ignore[arg-type]
    tx2 = sign_tempo_transaction(**{**kwargs_base, "nonce": 1})  # type: ignore[arg-type]
    assert tx_hash(tx1) != tx_hash(tx2)
