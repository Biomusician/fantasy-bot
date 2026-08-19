"""One-time Yahoo OAuth2 setup — run in two steps since this environment
can't do the interactive browser-popup flow yfpy/yahoo_oauth normally uses
(it blocks on a terminal `input()` call mid-process, which doesn't work
over a non-interactive tool call).

Instead we do the same two HTTP calls yahoo_oauth makes internally, just
split across two runs of this script:

Step 1 — get an authorize URL to open yourself:
    .venv/Scripts/python.exe scripts/yahoo_oauth_setup.py start \
        --client-id YOUR_CLIENT_ID --client-secret YOUR_CLIENT_SECRET

Step 2 — after approving in your browser, Yahoo shows a short verifier
code on screen. Paste it back in:
    .venv/Scripts/python.exe scripts/yahoo_oauth_setup.py finish \
        --client-id YOUR_CLIENT_ID --client-secret YOUR_CLIENT_SECRET \
        --code THE_CODE_YAHOO_SHOWED_YOU

This writes data/yahoo_token.json, which yahoo_client.py reads on every
subsequent run — refreshing the access token automatically when it expires
(refresh tokens don't expire under normal use), so this two-step dance is
only needed once.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from sleeper_tool.console import ensure_utf8_stdout

ensure_utf8_stdout()

AUTHORIZE_URL = "https://api.login.yahoo.com/oauth2/request_auth"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
REDIRECT_URI = "oob"  # out-of-band: Yahoo shows the code on-screen instead of redirecting
TOKEN_PATH = Path(__file__).resolve().parent.parent / "data" / "yahoo_token.json"


def cmd_start(client_id: str) -> None:
    url = f"{AUTHORIZE_URL}?client_id={client_id}&redirect_uri={REDIRECT_URI}&response_type=code"
    print("Open this URL in your browser, log into Yahoo, and approve access:\n")
    print(f"  {url}\n")
    print("Yahoo will then show you a short verifier code. Run this script again with:")
    print("  scripts/yahoo_oauth_setup.py finish --client-id ... --client-secret ... --code THE_CODE")


def cmd_finish(client_id: str, client_secret: str, code: str) -> None:
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    headers = {"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"}
    data = {"code": code, "redirect_uri": REDIRECT_URI, "grant_type": "authorization_code"}

    resp = requests.post(TOKEN_URL, headers=headers, data=data, timeout=20)
    if not resp.ok:
        print(f"Token exchange failed: {resp.status_code} {resp.text}", file=sys.stderr)
        sys.exit(1)

    payload = resp.json()
    token_data = {
        "access_token": payload["access_token"],
        "token_type": payload.get("token_type", "bearer"),
        "refresh_token": payload["refresh_token"],
        "token_time": time.time(),
        "guid": payload.get("xoauth_yahoo_guid"),
        "consumer_key": client_id,
        "consumer_secret": client_secret,
    }
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(json.dumps(token_data, indent=2), encoding="utf-8")
    print(f"Saved Yahoo access token to {TOKEN_PATH}. You're set — future runs won't need this step again.")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    start_p = sub.add_parser("start")
    start_p.add_argument("--client-id", required=True)

    finish_p = sub.add_parser("finish")
    finish_p.add_argument("--client-id", required=True)
    finish_p.add_argument("--client-secret", required=True)
    finish_p.add_argument("--code", required=True)

    args = parser.parse_args()
    if args.command == "start":
        cmd_start(args.client_id)
    else:
        cmd_finish(args.client_id, args.client_secret, args.code)


if __name__ == "__main__":
    main()
