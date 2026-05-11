"""AES-GCM-256 + PBKDF2-HMAC-SHA256 secret encryption.

Port of memo `CryptoVault.swift` (PR #7 SEC-IOS-020) into Python via
`cryptography.hazmat.primitives`. The contract is identical so a
secret encrypted in this module is binary-comparable to a secret
encrypted in the iOS CryptoVault — useful if the team ever ships a
cross-platform shared-secret design.

Versioning:

* `algorithm_version` (currently `2`) selects PBKDF2 iteration count.
  v1 = 200 000 (memo legacy), v2 = 600 000 (OWASP 2023). Both readers
  are kept alive so old ciphertexts decrypt during a rolling upgrade.
* `key_version` (currently `1`) maps to a `FUNDS_DASHBOARD_MASTER_KEY`
  / `FUNDS_DASHBOARD_MASTER_KEY_V2` / … env var. Rotation re-encrypts
  every `secret_config` row from `Vn` to `Vn+1`.

Wire format the caller sees::

    EncryptedSecret(
        ciphertext: bytes,       # auth-tag appended (AESGCM convention)
        nonce: bytes,            # 12 bytes
        salt: bytes,             # 16 bytes
        algorithm_version: int,
        key_version: int,
    )
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


# OWASP 2023 minimum (matches memo CryptoVault v2).
PBKDF2_ITERATIONS_V1 = 200_000
PBKDF2_ITERATIONS_V2 = 600_000
CURRENT_ALGORITHM_VERSION = 2

NONCE_SIZE = 12  # AES-GCM standard
SALT_SIZE = 16
KEY_SIZE = 32    # AES-256

# Env var template per key generation. `FUNDS_DASHBOARD_MASTER_KEY` is
# the bootstrap (= key_version 1). Rotation introduces
# `FUNDS_DASHBOARD_MASTER_KEY_V2`, etc.
_KEY_ENV_TEMPLATE = "FUNDS_DASHBOARD_MASTER_KEY{suffix}"
CURRENT_KEY_VERSION = 1


_ITERATION_TABLE = {
    1: PBKDF2_ITERATIONS_V1,
    2: PBKDF2_ITERATIONS_V2,
}


class CryptoError(Exception):
    """Encrypt / decrypt failure surface."""


@dataclass(frozen=True)
class EncryptedSecret:
    """Self-describing ciphertext bundle.

    A row in `secret_config` stores each field as a separate column so
    the encryption envelope is queryable (e.g. "which rows were
    encrypted under key_version 1?" → straight SQL).
    """

    ciphertext: bytes
    nonce: bytes
    salt: bytes
    algorithm_version: int
    key_version: int


def _master_key_env_name(key_version: int) -> str:
    if key_version == 1:
        return _KEY_ENV_TEMPLATE.format(suffix="")
    return _KEY_ENV_TEMPLATE.format(suffix=f"_V{key_version}")


def _load_master_key(key_version: int) -> bytes:
    """Read the master key for `key_version` from the env.

    The bootstrap key (`FUNDS_DASHBOARD_MASTER_KEY`) is required for
    the app to start at all (see `main.create_app` fail-closed gate);
    rotation versions are optional and surface a clear error when a
    decrypt request lands without the matching key set.
    """
    name = _master_key_env_name(key_version)
    raw = os.environ.get(name)
    if not raw:
        raise CryptoError(
            f"master key {name!r} is not configured. "
            "Rotation rows require their generation's key to be set "
            "in the environment before decrypt can succeed."
        )
    return raw.encode("utf-8")


def _derive_key(master_key: bytes, salt: bytes, algorithm_version: int) -> bytes:
    iterations = _ITERATION_TABLE.get(algorithm_version)
    if iterations is None:
        raise CryptoError(
            f"unknown algorithm_version {algorithm_version}; "
            f"supported: {sorted(_ITERATION_TABLE)}"
        )
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=KEY_SIZE,
        salt=salt,
        iterations=iterations,
    )
    return kdf.derive(master_key)


def encrypt(
    plaintext: str,
    *,
    algorithm_version: int = CURRENT_ALGORITHM_VERSION,
    key_version: int = CURRENT_KEY_VERSION,
) -> EncryptedSecret:
    """Encrypt `plaintext` under the current master key.

    The returned bundle is self-describing — callers persist every
    field as a separate column on `secret_config` so decrypt knows
    which iteration count + master-key generation to use.
    """
    if not isinstance(plaintext, str):
        raise CryptoError(
            f"encrypt expects str plaintext, got {type(plaintext).__name__}"
        )
    master_key = _load_master_key(key_version)
    salt = os.urandom(SALT_SIZE)
    nonce = os.urandom(NONCE_SIZE)
    derived = _derive_key(master_key, salt, algorithm_version)
    aesgcm = AESGCM(derived)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), associated_data=None)
    return EncryptedSecret(
        ciphertext=ciphertext,
        nonce=nonce,
        salt=salt,
        algorithm_version=algorithm_version,
        key_version=key_version,
    )


def decrypt(bundle: EncryptedSecret) -> str:
    """Reverse `encrypt`. Raises `CryptoError` on tag mismatch / missing key.

    Accepts any historical `algorithm_version` / `key_version` listed
    in the version tables — that's the rotation story.
    """
    master_key = _load_master_key(bundle.key_version)
    derived = _derive_key(master_key, bundle.salt, bundle.algorithm_version)
    aesgcm = AESGCM(derived)
    try:
        plaintext = aesgcm.decrypt(
            bundle.nonce, bundle.ciphertext, associated_data=None
        )
    except Exception as exc:  # cryptography raises InvalidTag (subclass)
        raise CryptoError(
            "decrypt failed — wrong master key, corrupt ciphertext, or "
            "tampered nonce/salt. Treat as a security event and audit "
            "the secret_config row's key_version + algorithm_version."
        ) from exc
    return plaintext.decode("utf-8")


def mask(plaintext: str) -> str:
    """Return a display-safe masked form of a secret.

    Used by `GET /api/v1/config/status` so the frontend can show
    "already configured · ****<last-4>" without ever transporting the
    plaintext to the browser.

    Empty or short (<8 char) values display as `(empty)` /
    `(short)` rather than leaking the entire string via the mask.
    """
    if not plaintext:
        return "(empty)"
    if len(plaintext) < 8:
        return "(short)"
    return f"****{plaintext[-4:]}"
