#!/usr/bin/env python3
"""Authorize Google Calendar read-only access and write the token file."""
from __future__ import annotations

import os
import sys
import argparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config
from bot.google_calendar import begin_oauth_flow, finish_oauth_flow


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="localhost", help="Local callback bind host")
    parser.add_argument("--port", type=int, default=0, help="Local callback port; 0 chooses a random free port")
    parser.add_argument("--open-browser", action="store_true", help="Try to open the authorization URL automatically")
    parser.add_argument("--manual", action="store_true", help="Print an auth URL and ask you to paste the final callback URL")
    args = parser.parse_args()

    if not config.GCAL_CLIENT_SECRET_FILE or not os.path.exists(config.GCAL_CLIENT_SECRET_FILE):
        print(
            f"Client secret file not found: {config.GCAL_CLIENT_SECRET_FILE}\n"
            "Create a Desktop app OAuth client in Google Cloud Console and place the JSON there.",
            file=sys.stderr,
        )
        return 1

    try:
        flow, auth_url = begin_oauth_flow(redirect_port=args.port or 58679)
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 1

    if args.manual:
        print("Open this URL in your browser:")
        print(auth_url)
        callback_url = input("\nPaste the final localhost callback URL here: ").strip()
        try:
            token_file = finish_oauth_flow(flow, callback_url)
        except Exception as e:
            print(str(e), file=sys.stderr)
            return 1
    else:
        creds = flow.run_local_server(
            host=args.host,
            port=args.port,
            open_browser=args.open_browser,
        )
        os.makedirs(os.path.dirname(config.GCAL_TOKEN_FILE), exist_ok=True)
        with open(config.GCAL_TOKEN_FILE, "w", encoding="utf-8") as token:
            token.write(creds.to_json())
        token_file = config.GCAL_TOKEN_FILE
    print(f"Wrote Google Calendar token: {token_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
