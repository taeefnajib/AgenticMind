"""Sign-in, profile and user management (ADR-0023)."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..auth import SESSION_COOKIE, AuthError, user_json
from ..db import UserRow
from ..deps import Services, services
from ..security import current_user, require_admin

router = APIRouter(prefix="/api", tags=["auth"])


class SetupIn(BaseModel):
    username: str = Field(max_length=64)
    display_name: str = Field(max_length=200)
    password: str = Field(max_length=256)
    email: str | None = Field(default=None, max_length=320)


class LoginIn(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


class ProfileIn(BaseModel):
    display_name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=320)


class PasswordIn(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)


class NewUserIn(BaseModel):
    username: str = Field(max_length=64)
    display_name: str = Field(max_length=200)
    password: str = Field(max_length=256, description="A first password the person changes after signing in")
    role: Literal["admin", "member"] = "member"
    email: str | None = Field(default=None, max_length=320)


class UserPatchIn(BaseModel):
    display_name: str | None = Field(default=None, max_length=200)
    role: Literal["admin", "member"] | None = None
    active: bool | None = None
    password: str | None = Field(default=None, max_length=256, description="Reset the password")


def _raise(exc: AuthError) -> HTTPException:
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
    return HTTPException(exc.status, {"code": exc.code, "message": str(exc)}, headers=headers)


def _signed_in(svc: Services, request: Request) -> UserRow:
    user = current_user(request)
    if user is None:
        if svc.settings.auth_mode == "off":
            message = "Sign-in is turned off (AGENTICMIND_AUTH=off): there is no profile."
            raise HTTPException(409, {"code": "AUTH_OFF", "message": message})
        raise HTTPException(401, {"code": "AUTH_REQUIRED", "message": "Sign in to continue."})
    return user


def _start_session(svc: Services, request: Request, response: Response, user: UserRow) -> None:
    token = svc.auth.create_session(user.id, user_agent=request.headers.get("user-agent", ""))
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(svc.auth.session_ttl.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=svc.settings.cookie_secure,
        path="/",
    )


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# ---- sign-in -----------------------------------------------------------------------------------
@router.get("/auth/status")
def status(request: Request, svc: Services = Depends(services)) -> dict[str, Any]:
    if svc.settings.auth_mode == "off":
        return {"mode": "off", "setup_required": False, "user": None}
    user = current_user(request)
    return {
        "mode": "required",
        "setup_required": svc.auth.count_users() == 0,
        "user": user_json(user) if user else None,
    }


@router.post("/auth/setup", status_code=201)
def setup(body: SetupIn, request: Request, response: Response, svc: Services = Depends(services)) -> dict[str, Any]:
    """Create the first account (an admin) on a new workspace, and sign in as it."""
    if svc.settings.auth_mode == "off":
        raise HTTPException(409, {"code": "AUTH_OFF", "message": "Sign-in is turned off."})
    try:
        user = svc.auth.create_user(
            username=body.username,
            display_name=body.display_name,
            password=body.password,
            email=body.email,
            role="admin",
            only_if_first=True,
        )
    except AuthError as exc:
        raise _raise(exc) from exc
    _start_session(svc, request, response, user)
    return user_json(user)


@router.post("/auth/login")
def login(body: LoginIn, request: Request, response: Response, svc: Services = Depends(services)) -> dict[str, Any]:
    if svc.settings.auth_mode == "off":
        raise HTTPException(409, {"code": "AUTH_OFF", "message": "Sign-in is turned off."})
    try:
        user = svc.auth.authenticate(body.username, body.password, client=_client(request))
    except AuthError as exc:
        raise _raise(exc) from exc
    svc.auth.revoke(request.cookies.get(SESSION_COOKIE))  # a fresh session id on every sign-in
    _start_session(svc, request, response, user)
    return user_json(user)


@router.post("/auth/logout", status_code=204)
def logout(request: Request, response: Response, svc: Services = Depends(services)) -> Response:
    svc.auth.revoke(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="lax", secure=svc.settings.cookie_secure)
    response.status_code = 204
    return response


# ---- own profile -------------------------------------------------------------------------------
@router.get("/auth/me")
def me(request: Request, svc: Services = Depends(services)) -> dict[str, Any]:
    user = _signed_in(svc, request)
    return {**user_json(user), "sessions": svc.auth.session_count(user.id)}


@router.patch("/auth/me")
def update_me(body: ProfileIn, request: Request, svc: Services = Depends(services)) -> dict[str, Any]:
    user = _signed_in(svc, request)
    changes = body.model_dump(exclude_unset=True)
    try:
        return user_json(svc.auth.update_user(user.id, **changes))
    except AuthError as exc:
        raise _raise(exc) from exc


@router.post("/auth/me/password", status_code=204)
def change_password(body: PasswordIn, request: Request, svc: Services = Depends(services)) -> Response:
    """Change your password; every other signed-in browser is signed out."""
    user = _signed_in(svc, request)
    try:
        svc.auth.change_password(
            user.id, body.current_password, body.new_password, keep_token=request.cookies.get(SESSION_COOKIE)
        )
    except AuthError as exc:
        raise _raise(exc) from exc
    return Response(status_code=204)


@router.post("/auth/me/sessions/revoke-others")
def revoke_other_sessions(request: Request, svc: Services = Depends(services)) -> dict[str, int]:
    user = _signed_in(svc, request)
    return {"revoked": svc.auth.revoke_all(user.id, except_token=request.cookies.get(SESSION_COOKIE))}


# ---- user management (admins) ------------------------------------------------------------------
@router.get("/users", dependencies=[Depends(require_admin)])
def list_users(svc: Services = Depends(services)) -> list[dict[str, Any]]:
    return [user_json(u) for u in svc.auth.list_users()]


@router.post("/users", status_code=201, dependencies=[Depends(require_admin)])
def create_user(body: NewUserIn, svc: Services = Depends(services)) -> dict[str, Any]:
    if svc.settings.auth_mode == "off":
        raise HTTPException(409, {"code": "AUTH_OFF", "message": "Turn sign-in on to add people."})
    try:
        return user_json(svc.auth.create_user(**body.model_dump()))
    except AuthError as exc:
        raise _raise(exc) from exc


@router.patch("/users/{user_id}")
def update_user(
    user_id: str, body: UserPatchIn, admin: UserRow | None = Depends(require_admin), svc: Services = Depends(services)
) -> dict[str, Any]:
    changes = body.model_dump(exclude_unset=True)
    if admin is not None and admin.id == user_id and changes.get("active") is False:
        raise HTTPException(409, {"code": "SELF_DEACTIVATE", "message": "You cannot deactivate your own account."})
    try:
        return user_json(svc.auth.update_user(user_id, **changes))
    except AuthError as exc:
        raise _raise(exc) from exc
