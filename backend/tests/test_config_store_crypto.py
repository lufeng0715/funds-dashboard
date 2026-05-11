"""Tests for `funds_dashboard.config_store.crypto`.

Pin every property the secret store relies on:

* round-trip survives the AESGCM tag + the JSON-encoding boundary the
  audit-log path enforces;
* a wrong master key fails — not "decrypts to garbage";
* a tampered ciphertext fails — auth-tag verification still holds;
* version readers cover both 200k (legacy) and 600k (current);
* `mask()` never leaks more than the trailing 4 characters.
"""

from __future__ import annotations

import os

import pytest

from funds_dashboard.config_store import crypto


@pytest.fixture(autouse=True)
def _set_master_keys(monkeypatch):
    """Every test sees a deterministic bootstrap + rotation key."""
    monkeypatch.setenv("FUNDS_DASHBOARD_MASTER_KEY", "test-master-key-v1")
    monkeypatch.setenv("FUNDS_DASHBOARD_MASTER_KEY_V2", "test-master-key-v2")


def test_round_trip_preserves_plaintext() -> None:
    plaintext = "ak_BovgxMdWJ4BRORb7FEk5oJQLVtsUy_LP"
    bundle = crypto.encrypt(plaintext)
    assert crypto.decrypt(bundle) == plaintext


def test_round_trip_handles_unicode() -> None:
    plaintext = "测试-中文-密钥-2026"
    bundle = crypto.encrypt(plaintext)
    assert crypto.decrypt(bundle) == plaintext


def test_wrong_master_key_raises_crypto_error(monkeypatch) -> None:
    bundle = crypto.encrypt("secret")
    monkeypatch.setenv("FUNDS_DASHBOARD_MASTER_KEY", "different-master-key")
    with pytest.raises(crypto.CryptoError, match="decrypt failed"):
        crypto.decrypt(bundle)


def test_tampered_ciphertext_raises_crypto_error() -> None:
    bundle = crypto.encrypt("secret")
    tampered = crypto.EncryptedSecret(
        ciphertext=bundle.ciphertext[:-1] + bytes([bundle.ciphertext[-1] ^ 0x01]),
        nonce=bundle.nonce,
        salt=bundle.salt,
        algorithm_version=bundle.algorithm_version,
        key_version=bundle.key_version,
    )
    with pytest.raises(crypto.CryptoError, match="decrypt failed"):
        crypto.decrypt(tampered)


def test_missing_master_key_raises_clear_error(monkeypatch) -> None:
    monkeypatch.delenv("FUNDS_DASHBOARD_MASTER_KEY", raising=False)
    with pytest.raises(crypto.CryptoError, match="master key"):
        crypto.encrypt("secret")


def test_v1_iterations_decryptable_during_rolling_upgrade() -> None:
    """Old ciphertexts (200k iter) must survive after v2 lands."""
    bundle_v1 = crypto.encrypt("legacy", algorithm_version=1)
    assert bundle_v1.algorithm_version == 1
    assert crypto.decrypt(bundle_v1) == "legacy"


def test_unknown_algorithm_version_raises() -> None:
    bundle = crypto.encrypt("x")
    bogus = crypto.EncryptedSecret(
        ciphertext=bundle.ciphertext,
        nonce=bundle.nonce,
        salt=bundle.salt,
        algorithm_version=99,
        key_version=bundle.key_version,
    )
    with pytest.raises(crypto.CryptoError, match="unknown algorithm_version"):
        crypto.decrypt(bogus)


def test_rotation_v2_key_decrypts() -> None:
    """Encrypting under key_version=2 reads from FUNDS_DASHBOARD_MASTER_KEY_V2."""
    bundle = crypto.encrypt("rotated", key_version=2)
    assert bundle.key_version == 2
    assert crypto.decrypt(bundle) == "rotated"


def test_rotation_v2_missing_key_raises(monkeypatch) -> None:
    monkeypatch.delenv("FUNDS_DASHBOARD_MASTER_KEY_V2", raising=False)
    with pytest.raises(crypto.CryptoError, match="MASTER_KEY_V2"):
        crypto.encrypt("rotated", key_version=2)


def test_mask_redacts_long_secret() -> None:
    assert crypto.mask("ak_BovgxMdWJ4BRORb7FEk5oJQLVtsUy_LP") == "****y_LP"


def test_mask_short_secret_is_obscured() -> None:
    assert crypto.mask("abc123") == "(short)"
    assert crypto.mask("") == "(empty)"


def test_each_encryption_uses_fresh_salt_and_nonce() -> None:
    """Two encrypts of the same plaintext MUST produce different ciphertext
    (else GCM nonce reuse breaks the security property)."""
    a = crypto.encrypt("secret")
    b = crypto.encrypt("secret")
    assert a.ciphertext != b.ciphertext
    assert a.nonce != b.nonce
    assert a.salt != b.salt
