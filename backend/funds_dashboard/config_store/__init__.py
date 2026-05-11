"""Encrypted runtime configuration store.

The store backs the Phase 0.5 config-Web page. Two SQLAlchemy tables
(`secret_config` for keys / passwords / tokens, `runtime_config` for
non-sensitive scalars + lists) plus a `config_audit_log` track of
every write. Mutations always go through the API layer
(`api/v1/config/*`) so the auth + audit chain is impossible to
bypass.

Encryption design (Vera consistency_checks §5 + §12, Nova msg=f6fe18bb
port from memo `CryptoVault.swift` PR #7 SEC-IOS-020):

* AES-GCM-256 + PBKDF2-HMAC-SHA256 600 000 iterations
* Per-row 16-byte salt, 12-byte nonce, 16-byte auth tag
* `algorithm_version` int → algorithm upgrade path (v1 200k → v2 600k)
* `key_version` int → master-key rotation without algorithm change

Both versions are integers in `secret_config` so rotation flows can
re-encrypt one row at a time without breaking the schema.
"""

__all__ = ["crypto"]
