"""Unit tests for EnvelopeKeystore."""

from __future__ import annotations

import base64
import os
import stat
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from routeweiler.budgets.keystore import EnvelopeKeystore
from routeweiler.errors import KeystoreAlreadyExistsError, KeystoreNotFoundError


@pytest.fixture
def ks(tmp_path: Path) -> EnvelopeKeystore:
    return EnvelopeKeystore(root=tmp_path / "keys")


def test_create_and_load_round_trip(ks: EnvelopeKeystore) -> None:
    key = ks.create("env_abc")
    loaded = ks.load("env_abc")
    # Verify the loaded key signs data that the original key would also sign.
    message = b"test"
    sig_original = key.sign(message)
    # Public key derived from loaded key must verify signature from original key.
    pub = loaded.public_key()
    pub.verify(sig_original, message)  # raises if invalid


def test_create_writes_0600_permissions(ks: EnvelopeKeystore) -> None:
    ks.create("env_perms")
    key_file = ks._key_path("env_perms")
    mode = stat.S_IMODE(os.stat(key_file).st_mode)
    assert mode == 0o600, f"Expected 0600, got {oct(mode)}"


def test_create_duplicate_raises(ks: EnvelopeKeystore) -> None:
    ks.create("env_dup")
    with pytest.raises(KeystoreAlreadyExistsError, match="env_dup"):
        ks.create("env_dup")


def test_load_missing_raises(ks: EnvelopeKeystore) -> None:
    with pytest.raises(KeystoreNotFoundError, match="env_missing"):
        ks.load("env_missing")


def test_exists_false_before_create(ks: EnvelopeKeystore) -> None:
    assert not ks.exists("env_new")


def test_exists_true_after_create(ks: EnvelopeKeystore) -> None:
    ks.create("env_exists")
    assert ks.exists("env_exists")


def test_public_key_b64_round_trips(ks: EnvelopeKeystore) -> None:
    ks.create("env_pub")
    b64 = ks.public_key_b64("env_pub")
    pub_bytes = base64.b64decode(b64)
    assert len(pub_bytes) == 32  # raw Ed25519 public key is 32 bytes
    # Must reconstruct a valid Ed25519 public key.
    Ed25519PublicKey.from_public_bytes(pub_bytes)


def test_different_envelopes_get_different_keys(ks: EnvelopeKeystore) -> None:
    b64_a = base64.b64encode(ks.create("env_a").public_key().public_bytes_raw()).decode()
    b64_b = base64.b64encode(ks.create("env_b").public_key().public_bytes_raw()).decode()
    assert b64_a != b64_b
