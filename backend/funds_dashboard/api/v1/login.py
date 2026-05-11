"""Login / logout endpoints.

`POST /api/v1/auth/login` accepts a `username` + `password`,
verifies the password against `FUNDS_DASHBOARD_ADMIN_PASSWORD_HASH`,
and (on success) sets an HttpOnly session cookie. `POST /api/v1/auth/logout`
clears it. Both 401 paths return identical generic messages so they
can't be used to enumerate valid usernames (Vera msg=408666c6).
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from ...auth import (
    SESSION_COOKIE_NAME,
    issue_session_cookie,
    verify_admin_password,
)
from ...config import Settings, get_settings


LOG = logging.getLogger(__name__)


# Single admin account in v0.5 — multi-admin is a future surface.
# `username` is checked (not just password) so audit-log entries
# show "admin:<username>" rather than a global "admin" actor; in v0.5
# the only valid username is "admin", but we keep the field for
# forward compatibility.
ADMIN_USERNAME = "admin"


router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=120)
    password: str = Field(..., min_length=1, max_length=200)


class LoginResponse(BaseModel):
    status: Literal["ok"]
    username: str


class LogoutResponse(BaseModel):
    status: Literal["ok"]


@router.post("/login", response_model=LoginResponse)
def login(
    body: LoginRequest,
    response: Response,
    settings: Settings = Depends(get_settings),
) -> LoginResponse:
    """Verify credentials, mint a session cookie on success.

    Generic 401 on any failure — never reveal which of "username
    unknown" / "password wrong" / "admin hash unset" / "bcrypt error"
    triggered the rejection.
    """
    if body.username != ADMIN_USERNAME or not verify_admin_password(
        body.password, stored_hash=settings.admin_password_hash
    ):
        # Log internally (so ops can see auth failures), generic
        # response (so callers can't enumerate).
        LOG.info("admin login failed for user=%r", body.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials.",
        )
    issue_session_cookie(response, settings, username=body.username)
    LOG.info("admin login ok for user=%r", body.username)
    return LoginResponse(status="ok", username=body.username)


@router.post("/logout", response_model=LogoutResponse)
def logout(response: Response) -> LogoutResponse:
    """Clear the session cookie. Idempotent — works even with no cookie."""
    response.delete_cookie(
        key=SESSION_COOKIE_NAME, path="/", samesite="strict", httponly=True
    )
    return LogoutResponse(status="ok")
