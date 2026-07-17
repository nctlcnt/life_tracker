"""Application-layer authentication for the dashboard and HTTP API."""

import hashlib
import hmac
import os
import time

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


API_KEY_ENV = "LIFE_TRACKER_API_KEY"
COOKIE_SECURE_ENV = "LIFE_TRACKER_COOKIE_SECURE"
SESSION_COOKIE = "life_tracker_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30
MIN_API_KEY_LENGTH = 32

PUBLIC_API_PATHS = {
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/session",
    "/api/calendar/oauth/callback",
}
PROTECTED_NON_API_PATHS = {"/docs", "/redoc", "/openapi.json"}


def secure_compare(candidate: str, expected: str) -> bool:
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def get_api_key() -> str | None:
    key = os.environ.get(API_KEY_ENV, "").strip()
    return key if len(key) >= MIN_API_KEY_LENGTH else None


def cookie_is_secure() -> bool:
    value = os.environ.get(COOKIE_SECURE_ENV, "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def session_token(api_key: str, issued_at: int | None = None) -> str:
    timestamp = issued_at if issued_at is not None else int(time.time())
    signature = hmac.new(
        api_key.encode("utf-8"),
        f"life-tracker-dashboard-session-v1:{timestamp}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{timestamp}.{signature}"


def session_token_is_valid(token: str, api_key: str) -> bool:
    try:
        raw_timestamp, _ = token.split(".", 1)
        issued_at = int(raw_timestamp)
    except (TypeError, ValueError):
        return False
    now = int(time.time())
    if issued_at > now + 60 or now - issued_at > SESSION_MAX_AGE:
        return False
    return hmac.compare_digest(token, session_token(api_key, issued_at))


def request_is_authenticated(request: Request, api_key: str) -> bool:
    header_key = request.headers.get("x-api-key", "")
    if header_key and secure_compare(header_key, api_key):
        return True

    cookie_token = request.cookies.get(SESSION_COOKIE, "")
    return bool(cookie_token) and session_token_is_valid(cookie_token, api_key)


def path_requires_auth(path: str) -> bool:
    if path in PUBLIC_API_PATHS:
        return False
    protects_docs = any(
        path == prefix or path.startswith(f"{prefix}/")
        for prefix in PROTECTED_NON_API_PATHS
    )
    return path == "/api" or path.startswith("/api/") or protects_docs


class ApiAuthMiddleware(BaseHTTPMiddleware):
    """Fail closed for all API and API-documentation requests."""

    async def dispatch(self, request: Request, call_next):
        if not path_requires_auth(request.url.path):
            return await call_next(request)

        api_key = get_api_key()
        if api_key is None:
            return JSONResponse(
                {"detail": f"API authentication is not configured; set {API_KEY_ENV}"},
                status_code=503,
            )

        if not request_is_authenticated(request, api_key):
            return JSONResponse({"detail": "authentication required"}, status_code=401)

        return await call_next(request)
