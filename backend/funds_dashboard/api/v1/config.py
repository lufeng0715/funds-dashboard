"""Config-Web endpoints for runtime settings and encrypted secrets."""

from __future__ import annotations

import json
import time
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...auth import SessionPayload, require_authenticated_admin
from ...config import Settings, get_settings
from ...config_store import crypto
from ...db import get_db_session
from ...db.models import ConfigAuditLog, RuntimeConfig, SecretConfig
from ...wind import WindClient, WindError
from ...wind.redact import redact_secrets


router = APIRouter(prefix="/config", tags=["config"])

SECRET_WIND_API_KEY = "wind_api_key"


class SecretStatus(BaseModel):
    configured: bool
    masked: str | None = None


class ConfigStatusResponse(BaseModel):
    secrets: dict[str, SecretStatus]
    runtime: dict[str, dict[str, Any]]


class SecretWriteRequest(BaseModel):
    value: str = Field(..., min_length=1, max_length=4096)


class SecretWriteResponse(BaseModel):
    name: str
    configured: Literal[True]
    masked: str


class SecretDeleteResponse(BaseModel):
    name: str
    deleted: bool


class RuntimeSectionWriteRequest(BaseModel):
    values: dict[str, Any] = Field(..., min_length=1)


class RuntimeSectionWriteResponse(BaseModel):
    section: str
    values: dict[str, Any]


class WindTestResponse(BaseModel):
    status: Literal["ok"]
    latency_ms: float


class AuditItem(BaseModel):
    action: str
    config_type: str
    config_name: str
    actor: str
    actor_ip: str | None
    timestamp: str
    details: dict[str, Any] | None


class AuditResponse(BaseModel):
    items: list[AuditItem]


def _actor(admin: SessionPayload) -> str:
    return f"admin:{admin.username}"


def _client_ip(request: Request) -> str | None:
    if request.client is None:
        return None
    return request.client.host


def _secret_bundle(row: SecretConfig) -> crypto.EncryptedSecret:
    return crypto.EncryptedSecret(
        ciphertext=row.ciphertext,
        nonce=row.nonce,
        salt=row.salt,
        algorithm_version=row.algorithm_version,
        key_version=row.key_version,
    )


def _load_secret_plaintext(session: Session, name: str) -> str | None:
    row = session.scalar(select(SecretConfig).where(SecretConfig.name == name))
    if row is None:
        return None
    return crypto.decrypt(_secret_bundle(row))


def _secret_status(session: Session, name: str) -> SecretStatus:
    row = session.scalar(select(SecretConfig).where(SecretConfig.name == name))
    if row is None:
        return SecretStatus(configured=False)
    return SecretStatus(
        configured=True,
        masked=crypto.mask(crypto.decrypt(_secret_bundle(row))),
    )


def _audit(
    session: Session,
    *,
    action: str,
    config_type: str,
    config_name: str,
    actor: str,
    actor_ip: str | None,
    details: dict[str, Any] | None = None,
) -> None:
    session.add(
        ConfigAuditLog(
            action=action,
            config_type=config_type,
            config_name=config_name,
            actor=actor,
            actor_ip=actor_ip,
            details=json.dumps(details or {}, sort_keys=True),
        )
    )


def _write_secret(
    session: Session,
    *,
    name: str,
    value: str,
    actor: str,
    actor_ip: str | None,
    audit_action: str | None = None,
) -> SecretWriteResponse:
    existing = session.scalar(select(SecretConfig).where(SecretConfig.name == name))
    bundle = crypto.encrypt(value)
    action = audit_action or ("update" if existing is not None else "create")
    if existing is None:
        existing = SecretConfig(
            name=name,
            ciphertext=bundle.ciphertext,
            nonce=bundle.nonce,
            salt=bundle.salt,
            algorithm_version=bundle.algorithm_version,
            key_version=bundle.key_version,
            updated_by=actor,
        )
        session.add(existing)
    else:
        existing.ciphertext = bundle.ciphertext
        existing.nonce = bundle.nonce
        existing.salt = bundle.salt
        existing.algorithm_version = bundle.algorithm_version
        existing.key_version = bundle.key_version
        existing.updated_by = actor

    masked = crypto.mask(value)
    _audit(
        session,
        action=action,
        config_type="secret",
        config_name=name,
        actor=actor,
        actor_ip=actor_ip,
        details={"masked": masked},
    )
    session.flush()
    return SecretWriteResponse(name=name, configured=True, masked=masked)


def seed_wind_key_from_env(
    session: Session, settings: Settings, *, actor_ip: str | None = None
) -> bool:
    """Seed the Wind key once from env into encrypted config storage."""
    if settings.wind_api_key is None:
        return False
    existing = session.scalar(
        select(SecretConfig).where(SecretConfig.name == SECRET_WIND_API_KEY)
    )
    if existing is not None:
        return False
    _write_secret(
        session,
        name=SECRET_WIND_API_KEY,
        value=settings.wind_api_key.get_secret_value(),
        actor="seeded_from_env",
        actor_ip=actor_ip,
        audit_action="seeded_from_env",
    )
    return True


def _runtime_status(session: Session) -> dict[str, dict[str, Any]]:
    rows = session.scalars(select(RuntimeConfig)).all()
    status_by_section: dict[str, dict[str, Any]] = {}
    for row in rows:
        if "." in row.name:
            section, key = row.name.split(".", 1)
        else:
            section, key = "default", row.name
        status_by_section.setdefault(section, {})[key] = json.loads(row.value)
    return status_by_section


@router.get("/status", response_model=ConfigStatusResponse)
def get_config_status(
    request: Request,
    _admin: SessionPayload = Depends(require_authenticated_admin),
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> ConfigStatusResponse:
    seed_wind_key_from_env(session, settings, actor_ip=_client_ip(request))
    return ConfigStatusResponse(
        secrets={SECRET_WIND_API_KEY: _secret_status(session, SECRET_WIND_API_KEY)},
        runtime=_runtime_status(session),
    )


@router.put("/sections/{section}", response_model=RuntimeSectionWriteResponse)
def put_runtime_section(
    section: str,
    body: RuntimeSectionWriteRequest,
    request: Request,
    admin: SessionPayload = Depends(require_authenticated_admin),
    session: Session = Depends(get_db_session),
) -> RuntimeSectionWriteResponse:
    actor = _actor(admin)
    actor_ip = _client_ip(request)
    for key, value in body.values.items():
        name = f"{section}.{key}"
        existing = session.scalar(select(RuntimeConfig).where(RuntimeConfig.name == name))
        action = "update" if existing is not None else "create"
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if existing is None:
            session.add(RuntimeConfig(name=name, value=encoded, updated_by=actor))
        else:
            existing.value = encoded
            existing.updated_by = actor
        _audit(
            session,
            action=action,
            config_type="runtime",
            config_name=name,
            actor=actor,
            actor_ip=actor_ip,
            details={"section": section, "key": key},
        )
    return RuntimeSectionWriteResponse(section=section, values=body.values)


@router.put("/secrets/{name}", response_model=SecretWriteResponse)
def put_secret(
    name: str,
    body: SecretWriteRequest,
    request: Request,
    admin: SessionPayload = Depends(require_authenticated_admin),
    session: Session = Depends(get_db_session),
) -> SecretWriteResponse:
    return _write_secret(
        session,
        name=name,
        value=body.value,
        actor=_actor(admin),
        actor_ip=_client_ip(request),
    )


@router.delete("/secrets/{name}", response_model=SecretDeleteResponse)
def delete_secret(
    name: str,
    request: Request,
    admin: SessionPayload = Depends(require_authenticated_admin),
    session: Session = Depends(get_db_session),
) -> SecretDeleteResponse:
    existing = session.scalar(select(SecretConfig).where(SecretConfig.name == name))
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Secret is not configured.",
        )
    session.delete(existing)
    _audit(
        session,
        action="delete",
        config_type="secret",
        config_name=name,
        actor=_actor(admin),
        actor_ip=_client_ip(request),
        details={"deleted": True},
    )
    return SecretDeleteResponse(name=name, deleted=True)


# Health-probe Wind tool. We use `fund_data:get_fund_quote` against
# the most stable Chinese ETF (`510300.SH`, CSI 300 — won't be
# delisted anytime soon) instead of the original
# `fund_data:get_fund_price_indicators` probe because the latter was
# returning `TOOL_ERROR: 服务暂时不可用，请稍后重试` for every call
# regardless of key validity, making the "测试连接" button useless to
# distinguish "bad key" from "Wind backend out". Alex msg=293ecab0
# captured the evidence (3/3 retries identical failure while
# `get_fund_quote` + `analytics_data:get_financial_data` worked against
# the same key).
#
# `get_fund_quote` is also a structured fund-data call (matches the
# original probe's intent), unlike NL-based `analytics_data` queries
# which would change the test's semantic meaning. The dev cost of
# pulling ~60 rows on a button click is negligible — this endpoint is
# admin-clicked, not polled.
_PROBE_TOOL = "fund_data:get_fund_quote"
_PROBE_PAYLOAD = {"windcode": "510300.SH"}


@router.post("/test/wind", response_model=WindTestResponse)
def test_wind_connection(
    request: Request,
    admin: SessionPayload = Depends(require_authenticated_admin),
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> WindTestResponse:
    seed_wind_key_from_env(session, settings, actor_ip=_client_ip(request))
    api_key = _load_secret_plaintext(session, SECRET_WIND_API_KEY)
    start = time.perf_counter()
    client = WindClient(
        node_path=settings.wind_cli_node_path,
        cli_script=settings.wind_cli_script,
        api_key=api_key,
    )
    try:
        client.call(_PROBE_TOOL, _PROBE_PAYLOAD)
    except WindError as exc:
        # Surface the underlying Wind error message so the operator
        # can tell "key rejected" from "backend out" — the old
        # generic `"Wind connection test failed."` string left feng-lu
        # (msg=22188ff4) unable to tell which case he was in.
        # Include stderr (CLI diagnostic text, already key-masked by the
        # CLI itself) so KEY_INVALID / 认证失败 errors are readable vs
        # the opaque "wind CLI exit 1" message.
        detail_parts = [f"Wind connection test failed: {exc}"]
        if exc.stderr:
            detail_parts.append(redact_secrets(exc.stderr[:500]))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="\n".join(detail_parts),
        ) from exc
    latency_ms = round((time.perf_counter() - start) * 1000, 3)
    _audit(
        session,
        action="test_connection",
        config_type="secret",
        config_name=SECRET_WIND_API_KEY,
        actor=_actor(admin),
        actor_ip=_client_ip(request),
        details={"status": "ok", "latency_ms": latency_ms},
    )
    return WindTestResponse(status="ok", latency_ms=latency_ms)


@router.get("/audit", response_model=AuditResponse)
def get_audit_log(
    _admin: SessionPayload = Depends(require_authenticated_admin),
    session: Session = Depends(get_db_session),
) -> AuditResponse:
    rows = session.scalars(
        select(ConfigAuditLog).order_by(ConfigAuditLog.timestamp.desc()).limit(100)
    ).all()
    return AuditResponse(
        items=[
            AuditItem(
                action=row.action,
                config_type=row.config_type,
                config_name=row.config_name,
                actor=row.actor,
                actor_ip=row.actor_ip,
                timestamp=row.timestamp.isoformat(),
                details=json.loads(row.details or "{}"),
            )
            for row in rows
        ]
    )
