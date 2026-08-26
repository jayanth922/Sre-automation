#!/usr/bin/env python3
"""Credential encryption round-trip and key-rotation tests."""

import base64
import hashlib
import hmac
import importlib.util
import sys
import types
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def crypto_module(monkeypatch):
    """Load backend.crypto with small SQLAlchemy/AESGCM test doubles."""
    sqlalchemy = types.ModuleType("sqlalchemy")
    sqlalchemy.Text = type("Text", (), {})
    sqlalchemy_types = types.ModuleType("sqlalchemy.types")
    sqlalchemy_types.TypeDecorator = type("TypeDecorator", (), {})

    class FakeAESGCM:
        def __init__(self, key):
            self.key = key

        def encrypt(self, nonce, plaintext, aad):
            encrypted = bytes(
                byte ^ self.key[index % len(self.key)]
                for index, byte in enumerate(plaintext)
            )
            tag = hmac.new(self.key, aad + nonce + encrypted, hashlib.sha256).digest()
            return encrypted + tag

        def decrypt(self, nonce, ciphertext, aad):
            encrypted, tag = ciphertext[:-32], ciphertext[-32:]
            expected = hmac.new(
                self.key, aad + nonce + encrypted, hashlib.sha256
            ).digest()
            if not hmac.compare_digest(tag, expected):
                raise ValueError("invalid tag")
            return bytes(
                byte ^ self.key[index % len(self.key)]
                for index, byte in enumerate(encrypted)
            )

    cryptography = types.ModuleType("cryptography")
    hazmat = types.ModuleType("cryptography.hazmat")
    primitives = types.ModuleType("cryptography.hazmat.primitives")
    ciphers = types.ModuleType("cryptography.hazmat.primitives.ciphers")
    aead = types.ModuleType("cryptography.hazmat.primitives.ciphers.aead")
    aead.AESGCM = FakeAESGCM

    for name, module in {
        "sqlalchemy": sqlalchemy,
        "sqlalchemy.types": sqlalchemy_types,
        "cryptography": cryptography,
        "cryptography.hazmat": hazmat,
        "cryptography.hazmat.primitives": primitives,
        "cryptography.hazmat.primitives.ciphers": ciphers,
        "cryptography.hazmat.primitives.ciphers.aead": aead,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    spec = importlib.util.spec_from_file_location(
        "_t09_crypto", _ROOT / "backend" / "crypto.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _key(byte: int) -> str:
    return base64.urlsafe_b64encode(bytes([byte]) * 32).decode("ascii")


def test_encrypted_string_round_trip_and_no_plaintext(
    crypto_module, monkeypatch
):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", _key(1))
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY_VERSION", "1")
    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEYS", raising=False)

    encrypted = crypto_module.encrypt_value("database-secret")
    assert encrypted.startswith("enc:v1:")
    assert "database-secret" not in encrypted
    assert crypto_module.decrypt_value(encrypted) == "database-secret"

    column = crypto_module.EncryptedString()
    assert column.process_result_value(
        column.process_bind_param("column-secret", None), None
    ) == "column-secret"


def test_rotation_decrypts_old_values_and_encrypts_new_version(
    crypto_module, monkeypatch
):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", _key(1))
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY_VERSION", "1")
    old_ciphertext = crypto_module.encrypt_value("rotating-secret")

    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", _key(2))
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY_VERSION", "2")
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEYS", '{"1":"' + _key(1) + '"}')

    assert crypto_module.decrypt_value(old_ciphertext) == "rotating-secret"
    new_ciphertext = crypto_module.encrypt_value("rotating-secret")
    assert new_ciphertext.startswith("enc:v2:")
    assert crypto_module.decrypt_value(new_ciphertext) == "rotating-secret"


def test_missing_historical_key_fails_closed(crypto_module, monkeypatch):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", _key(1))
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY_VERSION", "1")
    old_ciphertext = crypto_module.encrypt_value("old-secret")

    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", _key(2))
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY_VERSION", "2")
    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEYS", raising=False)
    with pytest.raises(
        crypto_module.CredentialEncryptionError,
        match="No credential encryption key configured for v1",
    ):
        crypto_module.decrypt_value(old_ciphertext)


def test_plaintext_database_value_is_rejected(crypto_module):
    with pytest.raises(
        crypto_module.CredentialEncryptionError,
        match="Refusing plaintext credential",
    ):
        crypto_module.decrypt_value("plaintext")


def test_lookup_hash_is_stable_without_revealing_token(crypto_module):
    token = "cl_high_entropy_token"
    first = crypto_module.credential_lookup_hash(token)
    assert first == crypto_module.credential_lookup_hash(token)
    assert token not in first
    assert len(first) == 64
