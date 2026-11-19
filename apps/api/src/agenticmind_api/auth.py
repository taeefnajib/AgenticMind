"""Accounts, sign-in sessions and workspace settings (ADR-0023).

- Passwords are hashed with scrypt (stdlib, memory-hard) and a random salt per password.
- A session is a random 256-bit token kept in an HttpOnly cookie; the database stores only its SHA-256,
  so a leaked database cannot be replayed as a login.
- Sessions slide: each use within ``session_days`` extends them. Changing a password, deactivating an
  account or "sign out everywhere" revokes them.
- Failed sign-ins are rate limited per username and per client address (in memory).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import sessionmaker

from .db import AppSettingRow, SessionRow, UserRow, utcnow

ROLES = ("admin", "member")
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 8
MAX_PASSWORD = 256
SESSION_COOKIE = "am_session"


def session_cookie_name(host: str | None) -> str:
    """The session cookie's name for the host the browser talks to, e.g. ``am_session_5173``.

    Browsers share cookies between all ports of a host, so two AgenticMind instances on ``localhost``
    (or a dev server next to a deployed one) would otherwise overwrite each other's sign-in.
    """
    port = host.rsplit(":", 1)[1] if host and ":" in host and not host.endswith("]") else ""
    return f"{SESSION_COOKIE}_{port}" if port.isdigit() else SESSION_COOKIE


#: Refresh ``last_seen_at``/``expires_at`` at most this often (avoids a write per request).
TOUCH_EVERY = timedelta(minutes=5)

_N, _R, _P, _DKLEN = 2**14, 8, 1, 32


class AuthError(Exception):
    def __init__(self, status: int, code: str, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.retry_after = retry_after


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    b64 = base64.urlsafe_b64encode
    return f"scrypt${_N}${_R}${_P}${b64(salt).decode()}${b64(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.urlsafe_b64decode(digest)
        actual = hashlib.scrypt(
            password.encode(),
            salt=base64.urlsafe_b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


#: Verified against when the username is unknown, so both cases take the same time.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def normalise_username(username: str) -> str:
    name = username.strip().lower()
    if not USERNAME_RE.match(name):
        raise AuthError(
            422,
            "USERNAME_INVALID",
            "Usernames are 2-32 characters: lowercase letters, digits, '.', '_' or '-', starting with a letter "
            "or digit.",
        )
    return name


def check_password_policy(password: str, *, username: str = "") -> None:
    if len(password) < MIN_PASSWORD:
        raise AuthError(422, "PASSWORD_TOO_SHORT", f"Use at least {MIN_PASSWORD} characters.")
    if len(password) > MAX_PASSWORD:
        raise AuthError(422, "PASSWORD_TOO_LONG", f"Use at most {MAX_PASSWORD} characters.")
    if username and password.lower() == username.lower():
        raise AuthError(422, "PASSWORD_WEAK", "The password must not be the username.")


def _clean_email(email: str | None) -> str | None:
    if email is None or not email.strip():
        return None
    email = email.strip()
    if len(email) > 320 or not EMAIL_RE.match(email):
        raise AuthError(422, "EMAIL_INVALID", "That does not look like an email address.")
    return email


def _clean_name(name: str) -> str:
    name = " ".join(name.split())
    if not name or len(name) > 200:
        raise AuthError(422, "DISPLAY_NAME_INVALID", "Enter a display name (up to 200 characters).")
    return name


def user_json(row: UserRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "username": row.username,
        "display_name": row.display_name,
        "email": row.email,
        "role": row.role,
        "active": row.active,
        "created_at": _aware(row.created_at).isoformat(),
        "last_login_at": _aware(row.last_login_at).isoformat() if row.last_login_at else None,
    }


class LoginLimiter:
    """At most ``limit`` failed sign-ins per key within ``window`` seconds (keys: username, client address)."""

    def __init__(self, limit: int = 5, window: float = 15 * 60) -> None:
        self.limit = limit
        self.window = window
        self._fails: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._fails[key]
        while q and q[0] <= now - self.window:
            q.popleft()
        return q

    def check(self, *keys: str) -> None:
        now = time.monotonic()
        with self._lock:
            for key in keys:
                q = self._prune(key, now)
                if len(q) >= self.limit:
                    wait = int(q[0] + self.window - now) + 1
                    raise AuthError(
                        429,
                        "LOGIN_RATE_LIMITED",
                        f"Too many failed sign-ins. Try again in {max(1, wait // 60)} minute(s).",
                        retry_after=wait,
                    )

    def failed(self, *keys: str) -> None:
        now = time.monotonic()
        with self._lock:
            for key in keys:
                self._prune(key, now).append(now)

    def succeeded(self, *keys: str) -> None:
        with self._lock:
            for key in keys:
                self._fails.pop(key, None)


class AuthStore:
    def __init__(self, session_factory: sessionmaker, *, session_days: int = 14) -> None:
        self._sf = session_factory
        self.session_ttl = timedelta(days=session_days)
        self.limiter = LoginLimiter()

    # ---- users ---------------------------------------------------------------------------------
    def count_users(self) -> int:
        with self._sf() as s:
            return s.scalar(select(func.count()).select_from(UserRow)) or 0

    def list_users(self) -> list[UserRow]:
        with self._sf() as s:
            return list(s.scalars(select(UserRow).order_by(UserRow.created_at)))

    def get_user(self, user_id: str) -> UserRow:
        with self._sf() as s:
            row = s.get(UserRow, user_id)
        if row is None:
            raise AuthError(404, "USER_NOT_FOUND", "No such user.")
        return row

    def create_user(
        self,
        *,
        username: str,
        display_name: str,
        password: str,
        role: str = "member",
        email: str | None = None,
        only_if_first: bool = False,
    ) -> UserRow:
        name = normalise_username(username)
        check_password_policy(password, username=name)
        if role not in ROLES:
            raise AuthError(422, "ROLE_INVALID", f"Role must be one of {', '.join(ROLES)}.")
        row = UserRow(
            id=f"usr_{uuid.uuid4().hex[:16]}",
            username=name,
            display_name=_clean_name(display_name),
            email=_clean_email(email),
            password_hash=hash_password(password),
            role=role,
        )
        with self._sf.begin() as s:
            if only_if_first and (s.scalar(select(func.count()).select_from(UserRow)) or 0) > 0:
                raise AuthError(409, "SETUP_DONE", "This workspace already has an account. Sign in instead.")
            if s.scalar(select(UserRow).where(UserRow.username == name)) is not None:
                raise AuthError(409, "USERNAME_TAKEN", f"The username {name!r} is already taken.")
            s.add(row)
        return row

    def _active_admins(self, s: Any) -> int:
        q = select(func.count()).select_from(UserRow).where(UserRow.role == "admin", UserRow.active.is_(True))
        return s.scalar(q) or 0

    def update_user(self, user_id: str, **changes: Any) -> UserRow:
        """Apply profile/admin changes (display_name, email, role, active, password)."""
        with self._sf.begin() as s:
            row = s.get(UserRow, user_id)
            if row is None:
                raise AuthError(404, "USER_NOT_FOUND", "No such user.")
            was_admin = row.role == "admin" and row.active
            if changes.get("display_name") is not None:
                row.display_name = _clean_name(changes["display_name"])
            if "email" in changes:
                row.email = _clean_email(changes["email"])
            if changes.get("role") is not None:
                if changes["role"] not in ROLES:
                    raise AuthError(422, "ROLE_INVALID", f"Role must be one of {', '.join(ROLES)}.")
                row.role = changes["role"]
            if changes.get("active") is not None:
                row.active = bool(changes["active"])
            revoke = False
            if changes.get("password") is not None:
                check_password_policy(changes["password"], username=row.username)
                row.password_hash = hash_password(changes["password"])
                revoke = True
            if was_admin and not (row.role == "admin" and row.active):
                s.flush()
                if self._active_admins(s) == 0:
                    raise AuthError(409, "LAST_ADMIN", "The workspace needs at least one active admin.")
            if not row.active:
                revoke = True
            row.updated_at = utcnow()
            if revoke and not changes.get("_keep_session"):
                s.execute(delete(SessionRow).where(SessionRow.user_id == user_id))
        return row

    def change_password(self, user_id: str, current: str, new: str, *, keep_token: str | None) -> None:
        row = self.get_user(user_id)
        if not verify_password(current, row.password_hash):
            raise AuthError(403, "PASSWORD_WRONG", "The current password is not correct.")
        if current == new:
            raise AuthError(422, "PASSWORD_UNCHANGED", "Choose a password different from the current one.")
        self.update_user(user_id, password=new, _keep_session=True)
        self.revoke_all(user_id, except_token=keep_token)

    # ---- sign-in -------------------------------------------------------------------------------
    def authenticate(self, username: str, password: str, *, client: str) -> UserRow:
        name = username.strip().lower()
        keys = (f"user:{name}", f"addr:{client}")
        self.limiter.check(*keys)
        with self._sf() as s:
            row = s.scalar(select(UserRow).where(UserRow.username == name))
        ok = verify_password(password, row.password_hash if row else _DUMMY_HASH) and row is not None
        if not ok or not row.active:
            self.limiter.failed(*keys)
            raise AuthError(401, "LOGIN_FAILED", "Wrong username or password.")
        self.limiter.succeeded(f"user:{name}")
        return row

    def create_session(self, user_id: str, *, user_agent: str = "") -> str:
        token = secrets.token_urlsafe(32)
        now = utcnow()
        with self._sf.begin() as s:
            s.add(
                SessionRow(
                    token_hash=_token_hash(token),
                    user_id=user_id,
                    created_at=now,
                    last_seen_at=now,
                    expires_at=now + self.session_ttl,
                    user_agent=user_agent[:300],
                )
            )
            user = s.get(UserRow, user_id)
            if user is not None:
                user.last_login_at = now
            # opportunistic cleanup of expired sessions
            s.execute(delete(SessionRow).where(SessionRow.expires_at < now))
        return token

    def resolve(self, token: str | None) -> UserRow | None:
        """The active user a session token belongs to, or None (unknown, expired or deactivated)."""
        if not token:
            return None
        now = utcnow()
        with self._sf() as s:
            sess = s.get(SessionRow, _token_hash(token))
            if sess is None or _aware(sess.expires_at) <= now:
                return None
            user = s.get(UserRow, sess.user_id)
            if user is None or not user.active:
                return None
            if now - _aware(sess.last_seen_at) >= TOUCH_EVERY:
                sess.last_seen_at = now
                sess.expires_at = now + self.session_ttl
                s.commit()
            return user

    def revoke(self, token: str | None) -> None:
        if token:
            with self._sf.begin() as s:
                s.execute(delete(SessionRow).where(SessionRow.token_hash == _token_hash(token)))

    def revoke_all(self, user_id: str, *, except_token: str | None = None) -> int:
        with self._sf.begin() as s:
            q = delete(SessionRow).where(SessionRow.user_id == user_id)
            if except_token:
                q = q.where(SessionRow.token_hash != _token_hash(except_token))
            return s.execute(q).rowcount or 0

    def session_count(self, user_id: str) -> int:
        with self._sf() as s:
            q = (
                select(func.count())
                .select_from(SessionRow)
                .where(SessionRow.user_id == user_id, SessionRow.expires_at > utcnow())
            )
            return s.scalar(q) or 0

    # ---- workspace settings --------------------------------------------------------------------
    def get_setting(self, key: str) -> AppSettingRow | None:
        with self._sf() as s:
            return s.get(AppSettingRow, key)

    def set_setting(self, key: str, value: dict[str, Any] | None, *, user_id: str | None) -> None:
        with self._sf.begin() as s:
            row = s.get(AppSettingRow, key)
            if value is None:
                if row is not None:
                    s.delete(row)
                return
            if row is None:
                s.add(AppSettingRow(key=key, value=value, updated_by=user_id))
            else:
                row.value = value
                row.updated_at = utcnow()
                row.updated_by = user_id
