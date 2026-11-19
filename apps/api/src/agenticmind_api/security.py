"""Request guard for ``/api`` (ADR-0023).

- **Sign-in:** with ``AGENTICMIND_AUTH=required`` every ``/api`` request needs a valid session cookie,
  except health and the sign-in endpoints themselves. The signed-in user is put on ``request.state``.
- **Cross-site requests:** state-changing requests that carry an ``Origin`` (or ``Referer``) must come from
  this host or an allowed origin. SameSite=Lax alone is not enough on localhost, because every
  ``localhost:<port>`` page is the *same site* (e.g. a deployed app or any other local dev server).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .auth import SESSION_COOKIE, AuthStore
from .db import UserRow
from .settings import Settings

PUBLIC_PATHS = frozenset({"/api/health", "/api/auth/status", "/api/auth/login", "/api/auth/setup", "/api/auth/logout"})
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
USER_KEY = "am_user"


def _origin_allowed(request: Request, allowed: tuple[str, ...]) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        referer = request.headers.get("referer")
        if not referer:
            return True  # not a browser form/fetch from another page (CLI, tests, server-to-server)
        parsed = urlparse(referer)
        origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin in allowed:
        return True
    parsed = urlparse(origin)
    return bool(parsed.netloc) and parsed.netloc == request.headers.get("host")


class ApiGuard:
    def __init__(self, app: ASGIApp, *, auth: AuthStore, settings: Settings) -> None:
        self.app = app
        self.auth = auth
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        if request.method in UNSAFE_METHODS and not _origin_allowed(request, self.settings.cors_origins):
            response = JSONResponse(
                {"detail": {"code": "ORIGIN_REFUSED", "message": "Cross-site request refused."}}, status_code=403
            )
            await response(scope, receive, send)
            return
        user: UserRow | None = None
        if self.settings.auth_mode != "off":
            user = await run_in_threadpool(self.auth.resolve, request.cookies.get(SESSION_COOKIE))
            if user is None and scope["path"] not in PUBLIC_PATHS:
                response = JSONResponse(
                    {"detail": {"code": "AUTH_REQUIRED", "message": "Sign in to continue."}}, status_code=401
                )
                await response(scope, receive, send)
                return
        scope.setdefault("state", {})[USER_KEY] = user
        await self.app(scope, receive, send)


def current_user(request: Request) -> UserRow | None:
    """The signed-in user; None when sign-in is off (``AGENTICMIND_AUTH=off``)."""
    return getattr(request.state, USER_KEY, None)


def require_admin(request: Request) -> UserRow | None:
    """Admins only. With sign-in off there is no one to tell apart, so everyone may (single user)."""
    user = current_user(request)
    services: Any = request.app.state.services
    if services.settings.auth_mode == "off":
        return user
    if user is None or user.role != "admin":
        raise HTTPException(403, {"code": "ADMIN_REQUIRED", "message": "Only workspace admins can do this."})
    return user
