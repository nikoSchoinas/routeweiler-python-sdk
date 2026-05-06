"""Per-envelope Ed25519 keypair management.

Private keys are stored at ``~/.routeweiler/keys/env_<id>.ed25519`` (raw 32-byte
format, permissions 0600).  Public keys are written into the envelopes SQLite
row so rail adapters can verify DrawReceipts without accessing the key file.

Key rotation is not supported (§8.5).  If a key is compromised, revoke the
envelope and create a new one.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from routeweiler.errors import KeystoreAlreadyExistsError, KeystoreNotFoundError

_DEFAULT_KEYSTORE_ROOT = Path.home() / ".routeweiler" / "keys"


class EnvelopeKeystore:
    """Manages per-envelope Ed25519 keypairs on disk.

    Args:
        root: Directory that holds the key files.  Defaults to
              ``~/.routeweiler/keys``.  Tests should pass ``tmp_path / "keys"``.
    """

    def __init__(self, root: Path = _DEFAULT_KEYSTORE_ROOT) -> None:
        self._root = root

    def _key_path(self, envelope_id: str) -> Path:
        return self._root / f"env_{envelope_id}.ed25519"

    def exists(self, envelope_id: str) -> bool:
        return self._key_path(envelope_id).exists()

    def create(self, envelope_id: str) -> Ed25519PrivateKey:
        """Generate a new keypair and write the private key to disk (perms 0600).

        Raises:
            KeystoreAlreadyExistsError: A key file for this envelope already exists.
        """
        path = self._key_path(envelope_id)
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)

        private_key = Ed25519PrivateKey.generate()
        raw_bytes = private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())

        # O_EXCL guarantees atomicity and prevents umask races.
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise KeystoreAlreadyExistsError(
                f"Key for envelope '{envelope_id}' already exists at {path}. "
                "Revoke the envelope and create a new one — key rotation is not supported."
            ) from exc
        with os.fdopen(fd, "wb") as f:
            f.write(raw_bytes)

        return private_key

    def load(self, envelope_id: str) -> Ed25519PrivateKey:
        """Load and return the private key for the given envelope.

        Raises:
            KeystoreNotFoundError: No key file exists for this envelope id.
        """
        path = self._key_path(envelope_id)
        try:
            raw_bytes = path.read_bytes()
        except FileNotFoundError as exc:
            raise KeystoreNotFoundError(
                f"No key found for envelope '{envelope_id}' at {path}."
            ) from exc
        return Ed25519PrivateKey.from_private_bytes(raw_bytes)

    def delete(self, envelope_id: str) -> None:
        """Delete the private key file for the given envelope. Idempotent."""
        self._key_path(envelope_id).unlink(missing_ok=True)

    def public_key_b64(self, envelope_id: str) -> str:
        """Return the base64-encoded raw 32-byte public key for the envelope."""
        private_key = self.load(envelope_id)
        pub_bytes = private_key.public_key().public_bytes_raw()
        return base64.b64encode(pub_bytes).decode()
