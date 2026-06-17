#!/usr/bin/env python3
"""Authorize Google Calendar read-only access and write the token file."""
from __future__ import annotations

import os
import sys
import argparse
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config
from bot.google_calendar import SCOPES


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="localhost", help="Local callback bind host")
    parser.add_argument("--port", type=int, default=0, help="Local callback port; 0 chooses a random free port")
    parser.add_argument("--open-browser", action="store_true", help="Try to open the authorization URL automatically")
    parser.add_argument("--manual", action="store_true", help="Print an auth URL and ask you to paste the final callback URL")
    args = parser.parse_args()

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Missing google-auth-oauthlib. Run: pip install -r requirements.txt", file=sys.stderr)
        return 1

    if not config.GCAL_CLIENT_SECRET_FILE or not os.path.exists(config.GCAL_CLIENT_SECRET_FILE):
        print(
            f"Client secret file not found: {config.GCAL_CLIENT_SECRET_FILE}\n"
            "Create a Desktop app OAuth client in Google Cloud Console and place the JSON there.",
            file=sys.stderr,
        )
        return 1

    os.makedirs(os.path.dirname(config.GCAL_TOKEN_FILE), exist_ok=True)
    flow = InstalledAppFlow.from_client_secrets_file(config.GCAL_CLIENT_SECRET_FILE, SCOPES)
    if args.manual:
        port = args.port or 58679
        flow.redirect_uri = f"http://localhost:{port}/"
        auth_url, _state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        print("Open this URL in your browser:")
        print(auth_url)
        callback_url = input("\nPaste the final localhost callback URL here: ").strip()
        parsed = urlparse(callback_url)
        query = parse_qs(parsed.query)
        if "error" in query:
            print(f"OAuth error: {query['error'][0]}", file=sys.stderr)
            return 1
        code = (query.get("code") or [""])[0]
        if not code:
            print("No code= parameter found in callback URL.", file=sys.stderr)
            return 1
        flow.fetch_token(code=code)
        creds = flow.credentials
    else:
        creds = flow.run_local_server(
            host=args.host,
            port=args.port,
            open_browser=args.open_browser,
        )
    with open(config.GCAL_TOKEN_FILE, "w", encoding="utf-8") as token:
        token.write(creds.to_json())
    print(f"Wrote Google Calendar token: {config.GCAL_TOKEN_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
