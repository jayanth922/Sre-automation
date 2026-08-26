"""Authenticated encryption for credentials stored by SQLAlchemy.

Ciphertext format: ``enc:v<version>:<urlsafe-base64(nonce + ciphertext)>``.
The version is embedded so old values remain decryptable during key rotation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Dict, Optional

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator


_PREFIX = "enc:v"


class CredentialEncryptionError(RuntimeError):
    """Credential encryption is unavailable or a ciphertext is invalid."""


def current_key_version() -> int:
    raw = os.getenv("CREDENTIAL_ENCRYPTION_KEY_VERSION", "1")
    try:
        version = int(raw)
    except ValueError as exc:
        raise CredentialEncryptionError(
            "CREDENTIAL_ENCRYPTION_KEY_VERSION must be an integer"
        ) from exc
    if version < 1:
        raise CredentialEncryptionError(
            "CREDENTIAL_ENCRYPTION_KEY_VERSION must be positive"
        )
    return version


def _decode_key(encoded: str, version: int) -> bytes:
    try:
        key = base64.urlsafe_b64decode(encoded.encode("ascii"))
    except Exception as exc:
        raise CredentialEncryptionError(
            f"Credential encryption key v{version} is not URL-safe base64"
        ) from exc
    if len(key) != 32:
        raise CredentialEncryptionError(
            f"Credential encryption key v{version} must decode to 32 bytes"
        )
    return key


def credential_keyring() -> Dict[int, bytes]:
    """Load the current key plus optional historical rotation keys."""
    keys: Dict[int, str] = {}
    historical = os.getenv("CREDENTIAL_ENCRYPTION_KEYS", "").strip()
    if historical:
        try:
            parsed = json.loads(historical)
        except json.JSONDecodeError as exc:
            raise CredentialEncryptionError(
                "CREDENTIAL_ENCRYPTION_KEYS must be a JSON object"
            ) from exc
        if not isinstance(parsed, dict):
            raise CredentialEncryptionError(
                "CREDENTIAL_ENCRYPTION_KEYS must be a JSON object"
            )
        try:
            keys.update(
                {int(version): str(value) for version, value in parsed.items()}
            )
        except (TypeError, ValueError) as exc:
            raise CredentialEncryptionError(
                "CREDENTIAL_ENCRYPTION_KEYS versions must be integers"
            ) from exc

    current = os.getenv("CREDENTIAL_ENCRYPTION_KEY", "").strip()
    if current:
        keys[current_key_version()] = current
    return {version: _decode_key(value, version) for version, value in keys.items()}


def _aesgcm(key: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise CredentialEncryptionError(
            "cryptography is required for credential encryption"
        ) from exc
    return AESGCM(key)


def encrypt_value(value: Optional[str], *, version: Optional[int] = None) -> Optional[str]:
    if value is None:
        return None
    if value.startswith(_PREFIX):
        return value

    selected_version = version or current_key_version()
    key = credential_keyring().get(selected_version)
    if key is None:
        raise CredentialEncryptionError(
            f"No credential encryption key configured for v{selected_version}"
        )

    nonce = os.urandom(12)
    aad = f"sentinel-credential:v{selected_version}".encode("ascii")
    ciphertext = _aesgcm(key).encrypt(nonce, value.encode("utf-8"), aad)
    payload = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")
    return f"{_PREFIX}{selected_version}:{payload}"


def decrypt_value(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if not value.startswith(_PREFIX):
        raise CredentialEncryptionError("Refusing plaintext credential from database")

    try:
        version_text, payload = value[len(_PREFIX) :].split(":", 1)
        version = int(version_text)
        packed = base64.urlsafe_b64decode(payload.encode("ascii"))
        nonce, ciphertext = packed[:12], packed[12:]
    except Exception as exc:
        raise CredentialEncryptionError("Malformed encrypted credential") from exc
    if len(nonce) != 12 or not ciphertext:
        raise CredentialEncryptionError("Malformed encrypted credential")

    key = credential_keyring().get(version)
    if key is None:
        raise CredentialEncryptionError(
            f"No credential encryption key configured for v{version}"
        )
    aad = f"sentinel-credential:v{version}".encode("ascii")
    try:
        plaintext = _aesgcm(key).decrypt(nonce, ciphertext, aad)
    except Exception as exc:
        raise CredentialEncryptionError("Credential decryption failed") from exc
    return plaintext.decode("utf-8")


def credential_lookup_hash(value: str) -> str:
    """Hash a high-entropy opaque credential for indexed equality lookup."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class EncryptedString(TypeDecorator):
    """SQLAlchemy text type that encrypts on bind and decrypts on load."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt_value(value)

    def process_result_value(self, value, dialect):
        return decrypt_value(value)
