"""One environment-bootstrapped admin and encrypted, revocable browser sessions."""

import hashlib
import hmac
import json
import math
import os
import secrets
import threading
import time
from collections import OrderedDict, deque
from http.cookies import CookieError, SimpleCookie
from pathlib import Path

import fileio

from cryptography.fernet import Fernet, InvalidToken

COOKIE_NAME = "pz_session"
SESSION_SECONDS = 12 * 60 * 60
REMEMBER_SECONDS = 30 * 24 * 60 * 60


class AuthError(ValueError):
    pass


class RateLimited(AuthError):
    pass


class SessionStorageError(AuthError):
    pass


class Auth:
    def __init__(self, login, password, key, *, secure=False, sessions_file=None):
        if not isinstance(login, str) or not login.strip() or len(login) > 128:
            raise AuthError("Задайте PZ_ADMIN_LOGIN (от 1 до 128 символов)")
        if not isinstance(password, str) or not 12 <= len(password) <= 1024:
            raise AuthError("Задайте PZ_ADMIN_PASSWORD (от 12 до 1024 символов)")
        try:
            self._cipher = Fernet(key)
        except (ValueError, TypeError):
            raise AuthError(
                "PZ_AUTH_KEY должен быть ключом Fernet (32 байта в base64url)"
            ) from None
        self.login = login
        self.secure = secure
        self._salt = secrets.token_bytes(16)
        self._password_hash = self._hash(password)
        self._sessions = OrderedDict()
        self._remembered = set()
        self._sessions_file = Path(sessions_file) if sessions_file is not None else None
        # Stable across restarts, but changing any admin secret revokes stored sessions.
        self._identity = hmac.new(
            key.encode("ascii") if isinstance(key, str) else key,
            json.dumps([login, password], ensure_ascii=False).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        self._attempts = OrderedDict()
        self._global_attempts = deque()
        self._lock = threading.Lock()
        if self._sessions_file is not None:
            self._load_sessions()
            self._save_sessions(self._sessions, self._remembered)

    @classmethod
    def from_env(cls):
        secure = os.getenv("PZ_AUTH_COOKIE_SECURE", "false").lower()
        if secure not in ("true", "false"):
            raise AuthError("PZ_AUTH_COOKIE_SECURE должен быть true или false")
        return cls(
            os.getenv("PZ_ADMIN_LOGIN", ""),
            os.getenv("PZ_ADMIN_PASSWORD", ""),
            os.getenv("PZ_AUTH_KEY", ""),
            secure=secure == "true",
            sessions_file=Path(os.getenv("DASHBOARD_DIR", "/dashboard-data")) / "auth-sessions.bin",
        )

    def _load_sessions(self):
        try:
            with self._sessions_file.open("rb") as source:
                raw = source.read(65537)
        except FileNotFoundError:
            return
        except OSError:
            raise SessionStorageError("Не удалось прочитать сохранённые сессии пульта") from None
        try:
            if len(raw) > 65536:
                return
            data = json.loads(self._cipher.decrypt(raw))
            if data.get("version") != 1 or data.get("identity") != self._identity:
                return
            sessions = data["sessions"]
            if not isinstance(sessions, dict) or len(sessions) > 128:
                return
            now = time.time()
            for session_id, expires in sessions.items():
                if (
                    not isinstance(session_id, str)
                    or len(session_id) != 43
                    or type(expires) not in (int, float)
                    or not math.isfinite(expires)
                ):
                    return
            self._sessions.update(
                (session_id, expires)
                for session_id, expires in sessions.items()
                if now < expires <= now + REMEMBER_SECONDS
            )
            self._remembered.update(self._sessions)
        except (InvalidToken, ValueError, UnicodeError, KeyError, AttributeError, TypeError):
            # A rotated key or damaged store never grants access; replace it with an empty store.
            return

    def _save_sessions(self, sessions, remembered):
        if self._sessions_file is None:
            return
        data = self._cipher.encrypt(
            json.dumps(
                {
                    "version": 1,
                    "identity": self._identity,
                    "sessions": {
                        key: expires for key, expires in sessions.items() if key in remembered
                    },
                }
            ).encode("utf-8")
        )
        try:
            fileio.atomic_write(self._sessions_file, data, prefix=".auth-")
        except OSError:
            raise SessionStorageError("Не удалось сохранить сессию. Повторите попытку") from None

    def _hash(self, password):
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), self._salt, 600_000)

    def sign_in(self, login, password, peer, *, remember=False):
        if type(remember) is not bool:
            raise AuthError("remember должен быть true или false")
        # Count before hashing; the lock also bounds concurrent expensive password checks.
        with self._lock:
            now = time.monotonic()
            attempts = self._attempts.setdefault(peer, deque())
            self._attempts.move_to_end(peer)
            while len(self._attempts) > 256:
                self._attempts.popitem(last=False)
            for window in (attempts, self._global_attempts):
                while window and window[0] <= now - 60:
                    window.popleft()
            if len(attempts) >= 5 or len(self._global_attempts) >= 30:
                raise RateLimited("Слишком много попыток входа. Повторите через минуту")
            attempts.append(now)
            self._global_attempts.append(now)
            if (
                not isinstance(login, str)
                or not isinstance(password, str)
                or len(login) > 128
                or len(password) > 1024
            ):
                raise AuthError("Неверный логин или пароль")
            try:
                password_ok = hmac.compare_digest(self._hash(password), self._password_hash)
                login_ok = hmac.compare_digest(login.encode("utf-8"), self.login.encode("utf-8"))
            except UnicodeError:
                raise AuthError("Неверный логин или пароль") from None
            if not password_ok or not login_ok:
                raise AuthError("Неверный логин или пароль")
            session_id = secrets.token_urlsafe(32)
            # Bound memory and remove stale sessions without extending their lifetime.
            self._prune_sessions()
            sessions = self._sessions.copy()
            remembered = self._remembered.copy()
            sessions[session_id] = time.time() + (REMEMBER_SECONDS if remember else SESSION_SECONDS)
            if remember:
                remembered.add(session_id)
            while len(sessions) > 128:
                evicted, _ = sessions.popitem(last=False)
                remembered.discard(evicted)
            self._save_sessions(sessions, remembered)
            self._sessions, self._remembered = sessions, remembered
            return self._cipher.encrypt(session_id.encode("ascii")).decode("ascii")

    def _prune_sessions(self):
        now = time.time()
        for session_id, expires in list(self._sessions.items()):
            if expires <= now:
                self._sessions.pop(session_id, None)
                self._remembered.discard(session_id)

    def session(self, cookie_header):
        if not cookie_header or len(cookie_header) > 8192:
            return None
        try:
            cookies = SimpleCookie()
            cookies.load(cookie_header)
            token = cookies.get(COOKIE_NAME)
            if token is None:
                return None
            session_id = self._cipher.decrypt(
                token.value.encode("ascii"), ttl=REMEMBER_SECONDS
            ).decode("ascii")
            with self._lock:
                if session_id not in self._remembered:
                    self._cipher.decrypt(token.value.encode("ascii"), ttl=SESSION_SECONDS)
        except (CookieError, InvalidToken, ValueError, UnicodeError):
            return None
        return session_id if self.is_active(session_id) else None

    def is_active(self, session_id):
        """Recheck an already authenticated stream without decrypting its cookie."""
        with self._lock:
            expires = self._sessions.get(session_id)
            if expires is not None and expires > time.time():
                return True
            self._sessions.pop(session_id, None)
            self._remembered.discard(session_id)
            return False

    def sign_out(self, session_id):
        with self._lock:
            sessions = self._sessions.copy()
            remembered = self._remembered.copy()
            sessions.pop(session_id, None)
            remembered.discard(session_id)
            self._save_sessions(sessions, remembered)
            self._sessions, self._remembered = sessions, remembered

    def cookie(self, token="", *, clear=False, remember=False):
        cookies = SimpleCookie()
        cookies[COOKIE_NAME] = token
        cookie = cookies[COOKIE_NAME]
        cookie["path"] = "/"
        cookie["httponly"] = True
        cookie["samesite"] = "Strict"
        if clear:
            cookie["max-age"] = 0
        elif remember:
            cookie["max-age"] = REMEMBER_SECONDS
        if self.secure:
            cookie["secure"] = True
        return cookie.OutputString()
