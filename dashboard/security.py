"""Authentication, sessions, CSRF, role-based access, auditing and masking.

Design notes:

* Passwords are hashed with bcrypt; the plaintext never leaves the request scope.
* Session ids are opaque 256-bit random tokens stored server-side, so a stolen
  cookie can be revoked instantly and nothing is decodable client-side.
* Every state-changing request must carry the session's CSRF token in the
  ``X-CSRF-Token`` header. Cookies alone are never sufficient.
* Failed logins are rate limited per (username, IP) with a lockout window.
* Every privileged action is written to an append-only audit table.
"""
from __future__ import annotations

import hmac
import logging
import re
import secrets
from dataclasses import dataclass
from typing import Any

import bcrypt

from dashboard.db import DashboardDB
from polybot.utils.time_utils import now_s

log = logging.getLogger("polybot.dashboard.security")

ROLES = ("admin", "trader", "viewer")

#: What each role is allowed to do. Checked on the server for every request;
#: the UI hides what a role cannot do, but the server is the authority.
PERMISSIONS: dict[str, frozenset[str]] = {
    "admin": frozenset({
        "view", "control.paper", "control.live", "control.dashboard", "emergency",
        "backup", "export", "journal.write", "notify.manage", "users.manage",
        "config.view", "logs.view",
    }),
    "trader": frozenset({
        "view", "control.paper", "control.live", "emergency", "backup", "export",
        "journal.write", "notify.manage", "logs.view",
    }),
    "viewer": frozenset({"view"}),
}

_SESSION_ID_BYTES = 32
_CSRF_BYTES = 32


@dataclass(frozen=True)
class Principal:
    """The authenticated caller behind a request."""

    user_id: int
    username: str
    role: str
    sid: str
    csrf: str

    def can(self, permission: str) -> bool:
        return permission in PERMISSIONS.get(self.role, frozenset())


class AuthError(Exception):
    """Authentication or authorisation failure."""

    def __init__(self, message: str, status: int = 401) -> None:
        super().__init__(message)
        self.status = status


class SecurityManager:
    """Owns users, sessions and the audit trail."""

    def __init__(self, db: DashboardDB, *, session_ttl_s: int, idle_timeout_s: int,
                 max_attempts: int = 6, lockout_s: int = 300) -> None:
        self._db = db
        self._ttl = session_ttl_s
        self._idle = idle_timeout_s
        self._max_attempts = max_attempts
        self._lockout = lockout_s

    # ======================================================================
    # Users
    # ======================================================================
    def user_count(self) -> int:
        row = self._db.query_one("SELECT COUNT(*) AS n FROM users")
        return int(row["n"]) if row else 0

    def bootstrap_admin(self, username: str, password: str | None) -> str | None:
        """Create the first admin if no users exist. Returns a generated password.

        When no password is configured one is generated and returned exactly once,
        so an install never ends up with a guessable default account.
        """
        if self.user_count() > 0:
            return None
        generated = None
        if not password:
            generated = secrets.token_urlsafe(18)
            password = generated
        self.create_user(username, password, role="admin", must_change_pw=generated is not None)
        log.warning("bootstrapped initial admin user", extra={"username": username})
        return generated

    def create_user(self, username: str, password: str, *, role: str = "viewer",
                    must_change_pw: bool = False) -> int:
        username = _normalise_username(username)
        if role not in ROLES:
            raise AuthError(f"unknown role {role!r}", status=400)
        problem = password_problem(password)
        if problem:
            raise AuthError(problem, status=400)
        if self._db.query_one("SELECT id FROM users WHERE username = ?", (username,)):
            raise AuthError("username already exists", status=409)
        return self._db.execute(
            "INSERT INTO users (username, password_hash, role, created_ts, active, must_change_pw) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (username, hash_password(password), role, now_s(), 1 if must_change_pw else 0))

    def list_users(self) -> list[dict[str, Any]]:
        return self._db.query(
            "SELECT id, username, role, created_ts, last_login_ts, active, must_change_pw "
            "FROM users ORDER BY username")

    def set_role(self, user_id: int, role: str) -> None:
        if role not in ROLES:
            raise AuthError(f"unknown role {role!r}", status=400)
        if role != "admin" and self._is_last_admin(user_id):
            raise AuthError("cannot demote the last remaining admin", status=409)
        self._db.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))

    def set_active(self, user_id: int, active: bool) -> None:
        if not active and self._is_last_admin(user_id):
            raise AuthError("cannot disable the last remaining admin", status=409)
        self._db.execute("UPDATE users SET active = ? WHERE id = ?", (1 if active else 0, user_id))
        if not active:
            self.revoke_user_sessions(user_id)

    def delete_user(self, user_id: int) -> None:
        if self._is_last_admin(user_id):
            raise AuthError("cannot delete the last remaining admin", status=409)
        self.revoke_user_sessions(user_id)
        self._db.execute("DELETE FROM users WHERE id = ?", (user_id,))

    def change_password(self, user_id: int, new_password: str, *,
                        current_password: str | None = None) -> None:
        problem = password_problem(new_password)
        if problem:
            raise AuthError(problem, status=400)
        row = self._db.query_one("SELECT password_hash FROM users WHERE id = ?", (user_id,))
        if row is None:
            raise AuthError("user not found", status=404)
        if current_password is not None and not verify_password(current_password, row["password_hash"]):
            raise AuthError("current password is incorrect", status=403)
        self._db.execute(
            "UPDATE users SET password_hash = ?, must_change_pw = 0 WHERE id = ?",
            (hash_password(new_password), user_id))
        self.revoke_user_sessions(user_id)

    def _is_last_admin(self, user_id: int) -> bool:
        row = self._db.query_one("SELECT role FROM users WHERE id = ?", (user_id,))
        if not row or row["role"] != "admin":
            return False
        remaining = self._db.query_one(
            "SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND active = 1 AND id != ?",
            (user_id,))
        return int(remaining["n"]) == 0 if remaining else True

    # ======================================================================
    # Login
    # ======================================================================
    def is_locked_out(self, username: str, ip: str) -> float:
        """Seconds remaining in a lockout window, or 0 when not locked out."""
        since = now_s() - self._lockout
        row = self._db.query_one(
            "SELECT COUNT(*) AS n, MAX(ts) AS last FROM login_attempts "
            "WHERE success = 0 AND ts > ? AND (username = ? OR ip = ?)",
            (since, _normalise_username(username), ip))
        if not row or int(row["n"]) < self._max_attempts:
            return 0.0
        return max(0.0, self._lockout - (now_s() - float(row["last"] or 0)))

    def login(self, username: str, password: str, *, ip: str, user_agent: str) -> Principal:
        username = _normalise_username(username)
        locked = self.is_locked_out(username, ip)
        if locked > 0:
            self._record_attempt(username, ip, False)
            raise AuthError(f"too many failed attempts; try again in {int(locked)}s", status=429)

        row = self._db.query_one(
            "SELECT id, username, password_hash, role, active FROM users WHERE username = ?",
            (username,))
        # Always run a hash comparison so a missing user and a wrong password take
        # the same time and cannot be told apart.
        stored = row["password_hash"] if row else _DUMMY_HASH
        ok = verify_password(password, stored)
        if not row or not ok or not row["active"]:
            self._record_attempt(username, ip, False)
            raise AuthError("invalid username or password", status=401)

        self._record_attempt(username, ip, True)
        self._db.execute("UPDATE users SET last_login_ts = ? WHERE id = ?", (now_s(), row["id"]))
        return self._create_session(row, ip=ip, user_agent=user_agent)

    def _record_attempt(self, username: str, ip: str, success: bool) -> None:
        self._db.execute(
            "INSERT INTO login_attempts (ts, username, ip, success) VALUES (?, ?, ?, ?)",
            (now_s(), username, ip, 1 if success else 0))

    def _create_session(self, user: dict[str, Any], *, ip: str, user_agent: str) -> Principal:
        sid = secrets.token_urlsafe(_SESSION_ID_BYTES)
        csrf = secrets.token_urlsafe(_CSRF_BYTES)
        ts = now_s()
        self._db.execute(
            "INSERT INTO sessions (sid, user_id, csrf, created_ts, last_seen_ts, expires_ts, ip, "
            "user_agent) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, user["id"], csrf, ts, ts, ts + self._ttl, ip, user_agent[:300]))
        return Principal(user_id=int(user["id"]), username=user["username"],
                         role=user["role"], sid=sid, csrf=csrf)

    # ======================================================================
    # Session resolution
    # ======================================================================
    def resolve(self, sid: str | None, *, touch: bool = True) -> Principal | None:
        """Return the principal behind a session id, or ``None`` if invalid."""
        if not sid:
            return None
        row = self._db.query_one(
            "SELECT s.sid, s.csrf, s.expires_ts, s.last_seen_ts, u.id AS user_id, u.username, "
            "u.role, u.active FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.sid = ?",
            (sid,))
        if row is None:
            return None
        now = now_s()
        if now > float(row["expires_ts"]) or not row["active"]:
            self.revoke(sid)
            return None
        if self._idle > 0 and now - float(row["last_seen_ts"]) > self._idle:
            self.revoke(sid)
            return None
        if touch:
            self._db.execute("UPDATE sessions SET last_seen_ts = ? WHERE sid = ?", (now, sid))
        return Principal(user_id=int(row["user_id"]), username=row["username"], role=row["role"],
                         sid=row["sid"], csrf=row["csrf"])

    def verify_csrf(self, principal: Principal, token: str | None) -> None:
        if not token or not hmac.compare_digest(principal.csrf, token):
            raise AuthError("missing or invalid CSRF token", status=403)

    def require(self, principal: Principal | None, permission: str) -> Principal:
        if principal is None:
            raise AuthError("authentication required", status=401)
        if not principal.can(permission):
            raise AuthError(f"role '{principal.role}' is not permitted to {permission}", status=403)
        return principal

    def revoke(self, sid: str) -> None:
        self._db.execute("DELETE FROM sessions WHERE sid = ?", (sid,))

    def revoke_user_sessions(self, user_id: int) -> None:
        self._db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    def sessions_for(self, user_id: int) -> list[dict[str, Any]]:
        return self._db.query(
            "SELECT sid, created_ts, last_seen_ts, expires_ts, ip, user_agent FROM sessions "
            "WHERE user_id = ? ORDER BY last_seen_ts DESC", (user_id,))

    def purge_expired(self) -> None:
        self._db.execute("DELETE FROM sessions WHERE expires_ts < ?", (now_s(),))

    # ======================================================================
    # Audit
    # ======================================================================
    def audit(self, principal: Principal | None, action: str, *, target: str = "",
              detail: str = "", ip: str = "", ok: bool = True) -> None:
        self._db.execute(
            "INSERT INTO audit (ts, actor, role, action, target, detail, ip, ok) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (now_s(), principal.username if principal else "anonymous",
             principal.role if principal else "-", action, target, mask_secrets(detail), ip,
             1 if ok else 0))

    def audit_log(self, limit: int = 200) -> list[dict[str, Any]]:
        return self._db.query("SELECT * FROM audit ORDER BY ts DESC LIMIT ?", (limit,))


# ======================================================================
# Password helpers
# ======================================================================
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


#: Hash of a random value, compared against when a username does not exist so
#: that login timing does not reveal whether an account is real.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(24))


def password_problem(password: str) -> str | None:
    """Return why a password is unacceptable, or ``None`` when it is fine."""
    if len(password) < 12:
        return "password must be at least 12 characters"
    if len(password) > 256:
        return "password must be at most 256 characters"
    classes = sum(bool(re.search(pattern, password))
                  for pattern in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]"))
    if classes < 3:
        return "password must mix at least three of: lowercase, uppercase, digits, symbols"
    return None


def _normalise_username(username: str) -> str:
    return (username or "").strip().lower()[:64]


# ======================================================================
# Secret masking
# ======================================================================
# Anything 64 hex characters long is a private key or a signature.
_HEX_KEY = re.compile(r"(?:0x)?[0-9a-fA-F]{64}")

# `<anything>secret<anything> = value` and friends. The name part is matched
# loosely (``api_secret``, ``POLY_API_PASSPHRASE``, ``bot-token``) because the
# sensitive word is rarely a standalone token in real configuration keys.
_KEYED_SECRET = re.compile(
    r"(?i)([\w.\-]*(?:secret|passphrase|password|token|api[_-]?key|private[_-]?key)[\w.\-]*)"
    r"(\s*[:=]\s*)(?:[\"']?)([^\s\"',}\]]+)")


def mask_secrets(text: str) -> str:
    """Redact anything in ``text`` that looks like a credential.

    Applied to audit details and to log lines before they reach the browser, so a
    key accidentally written into a log is never re-published by the dashboard.
    """
    if not text:
        return text
    masked = _HEX_KEY.sub("«redacted-key»", text)
    return _KEYED_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}«redacted»", masked)


def mask_address(address: str, *, keep: int = 6) -> str:
    """Shorten an address for display: ``0x1234…abcd``."""
    if not address or len(address) <= keep * 2:
        return address
    return f"{address[:keep]}…{address[-4:]}"
