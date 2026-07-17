import os
import time
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from api.auth import (
    API_KEY_ENV,
    COOKIE_SECURE_ENV,
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    ApiAuthMiddleware,
    cookie_is_secure,
    get_api_key,
    path_requires_auth,
    request_is_authenticated,
    secure_compare,
    session_token,
    session_token_is_valid,
)


TEST_KEY = "k" * 32


class ApiAuthUnitTests(unittest.TestCase):
    def test_api_routes_are_protected_except_explicit_public_routes(self):
        self.assertTrue(path_requires_auth("/api/timeline"))
        self.assertTrue(path_requires_auth("/api/health"))
        self.assertTrue(path_requires_auth("/openapi.json"))
        self.assertTrue(path_requires_auth("/docs/"))
        self.assertFalse(path_requires_auth("/api/auth/session"))
        self.assertFalse(path_requires_auth("/api/calendar/oauth/callback"))
        self.assertFalse(path_requires_auth("/app/index.html"))

    def test_short_or_missing_key_is_not_configured(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(get_api_key())
        with patch.dict(os.environ, {API_KEY_ENV: "too-short"}, clear=True):
            self.assertIsNone(get_api_key())

    def test_session_token_is_signed_and_expires(self):
        now = int(time.time())
        token = session_token(TEST_KEY, now)
        self.assertTrue(session_token_is_valid(token, TEST_KEY))
        self.assertFalse(session_token_is_valid(token, "z" * 32))
        expired = session_token(TEST_KEY, now - SESSION_MAX_AGE - 1)
        self.assertFalse(session_token_is_valid(expired, TEST_KEY))

    def test_cookie_secure_defaults_true(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(cookie_is_secure())
        with patch.dict(os.environ, {COOKIE_SECURE_ENV: "false"}, clear=True):
            self.assertFalse(cookie_is_secure())

    def test_non_ascii_invalid_secret_is_rejected_without_error(self):
        self.assertFalse(secure_compare("错误密钥", TEST_KEY))


class ApiAuthMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app = FastAPI()
        app.add_middleware(ApiAuthMiddleware)

        @app.get("/api/private")
        async def private():
            return {"ok": True}

        @app.get("/api/calendar/oauth/callback")
        async def oauth_callback():
            return {"oauth": True}

        @app.get("/internal/health")
        async def internal_health():
            return {"status": "ok"}

        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_missing_configuration_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            response = await self.client.get("/api/private")
        self.assertEqual(response.status_code, 503)

    async def test_invalid_credentials_are_rejected(self):
        with patch.dict(os.environ, {API_KEY_ENV: TEST_KEY}, clear=True):
            response = await self.client.get("/api/private", headers={"X-API-Key": "wrong"})
        self.assertEqual(response.status_code, 401)

    async def test_header_and_cookie_authenticate(self):
        with patch.dict(os.environ, {API_KEY_ENV: TEST_KEY}, clear=True):
            header_response = await self.client.get(
                "/api/private",
                headers={"X-API-Key": TEST_KEY},
            )
            self.client.cookies.set(SESSION_COOKIE, session_token(TEST_KEY))
            cookie_response = await self.client.get("/api/private")
        self.assertEqual(header_response.status_code, 200)
        self.assertEqual(cookie_response.status_code, 200)

    async def test_oauth_and_internal_health_remain_public(self):
        with patch.dict(os.environ, {}, clear=True):
            oauth_response = await self.client.get("/api/calendar/oauth/callback")
            health_response = await self.client.get("/internal/health")
        self.assertEqual(oauth_response.status_code, 200)
        self.assertEqual(health_response.status_code, 200)


class ApiAuthRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from api.server import app

        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_login_cookie_unlocks_api_and_logout_revokes_it(self):
        env = {API_KEY_ENV: TEST_KEY, COOKIE_SECURE_ENV: "false"}
        with patch.dict(os.environ, env, clear=True):
            initial = await self.client.get("/api/auth/session")
            rejected = await self.client.post(
                "/api/auth/login",
                json={"api_key": "错误密钥"},
            )
            login = await self.client.post(
                "/api/auth/login",
                json={"api_key": TEST_KEY},
            )
            authenticated = await self.client.get("/api/version")
            logout = await self.client.post("/api/auth/logout")
            locked_again = await self.client.get("/api/version")

        self.assertEqual(initial.json(), {"authenticated": False, "configured": True})
        self.assertEqual(rejected.status_code, 401)
        self.assertEqual(login.status_code, 200)
        self.assertIn("HttpOnly", login.headers["set-cookie"])
        self.assertEqual(authenticated.status_code, 200)
        self.assertEqual(logout.status_code, 200)
        self.assertEqual(locked_again.status_code, 401)

    async def test_real_oauth_callback_is_not_replaced_by_auth_error(self):
        with patch.dict(os.environ, {}, clear=True):
            response = await self.client.get(
                "/api/calendar/oauth/callback",
                params={"error": "access_denied"},
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Google Calendar", response.text)


if __name__ == "__main__":
    unittest.main()
