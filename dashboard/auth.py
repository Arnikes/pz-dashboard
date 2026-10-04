"""One environment-bootstrapped admin and encrypted, revocable browser sessions."""

import hashlib
import hmac
import os
import secrets
import threading
import time
from collections import OrderedDict, deque
from http.cookies import CookieError, SimpleCookie

from cryptography.fernet import Fernet, InvalidToken

COOKIE_NAME = "pz_session"
SESSION_SECONDS = 12 * 60 * 60


class AuthError(ValueError):
    pass


class RateLimited(AuthError):
    pass


class Auth:
    def __init__(self, login, password, key, *, secure=False):
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
        self._attempts = OrderedDict()
        self._global_attempts = deque()
        self._lock = threading.Lock()

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
        )

    def _hash(self, password):
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), self._salt, 600_000)

    def sign_in(self, login, password, peer):
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
            self._sessions[session_id] = time.time() + SESSION_SECONDS
            # Bound memory and remove stale sessions without extending their lifetime.
            self._prune_sessions()
            while len(self._sessions) > 128:
                self._sessions.popitem(last=False)
            return self._cipher.encrypt(session_id.encode("ascii")).decode("ascii")

    def _prune_sessions(self):
        now = time.time()
        for session_id, expires in list(self._sessions.items()):
            if expires <= now:
                self._sessions.pop(session_id, None)

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
                token.value.encode("ascii"), ttl=SESSION_SECONDS
            ).decode("ascii")
        except (CookieError, InvalidToken, ValueError, UnicodeError):
            return None
        with self._lock:
            self._prune_sessions()
            return session_id if session_id in self._sessions else None

    def sign_out(self, session_id):
        with self._lock:
            self._sessions.pop(session_id, None)

    def cookie(self, token="", *, clear=False):
        cookies = SimpleCookie()
        cookies[COOKIE_NAME] = token
        cookie = cookies[COOKIE_NAME]
        cookie["path"] = "/"
        cookie["httponly"] = True
        cookie["samesite"] = "Strict"
        cookie["max-age"] = 0 if clear else SESSION_SECONDS
        if self.secure:
            cookie["secure"] = True
        return cookie.OutputString()
