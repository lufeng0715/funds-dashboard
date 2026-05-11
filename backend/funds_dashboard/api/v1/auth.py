"""Authentication dependencies for the v1 API.

Phase 0.5 wired the real implementation in `funds_dashboard.auth`
(bcrypt password verify + HMAC-signed session cookies). This module
re-exports the dependency so existing imports
(`from .auth import require_authenticated_admin`) continue to work
without touching every router. Single source of truth in
`funds_dashboard.auth.session`.
"""

from __future__ import annotations

from ...auth import require_authenticated_admin


__all__ = ["require_authenticated_admin"]
