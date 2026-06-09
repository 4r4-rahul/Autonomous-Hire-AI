#!/usr/bin/env python3
"""
One-time Gmail OAuth setup: generates GMAIL_REFRESH_TOKEN for the pipeline.

Usage:
    python tools/gmail_oauth_setup.py

Prerequisites:
    1. Go to https://console.cloud.google.com/
    2. Create a project (or use existing one)
    3. Enable the Gmail API: APIs & Services → Enable APIs → Gmail API
    4. Create OAuth 2.0 credentials:
         - APIs & Services → Credentials → Create Credentials → OAuth client ID
         - Application type: Desktop app
         - Download the JSON, or copy Client ID and Client Secret
    5. Run this script with your Client ID and Client Secret
"""

import http.server
import json
import os
import threading
import urllib.parse
import webbrowser
from pathlib import Path

import httpx

ENV_PATH = Path(__file__).parent.parent / ".env"
SCOPES = "https://www.googleapis.com/auth/gmail.readonly"
REDIRECT_URI = "http://localhost:8765"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def main():
    client_id = input("Enter GMAIL_CLIENT_ID: ").strip()
    client_secret = input("Enter GMAIL_CLIENT_SECRET: ").strip()

    if not client_id or not client_secret:
        print("Both Client ID and Client Secret are required.")
        return

    auth_params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
    }
    auth_url = f"{AUTH_URL}?{urllib.parse.urlencode(auth_params)}"

    received_code: list[str] = []
    stop_event = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            if "code" in params:
                received_code.append(params["code"][0])
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"<h2>Authorization successful! You can close this tab.</h2>")
                stop_event.set()
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"No code received.")

        def log_message(self, fmt, *args):
            pass

    server = http.server.HTTPServer(("localhost", 8765), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever())
    thread.daemon = True
    thread.start()

    print(f"\nOpening browser for Google authorization...")
    webbrowser.open(auth_url)
    print("If the browser doesn't open, visit this URL manually:")
    print(f"  {auth_url}\n")

    stop_event.wait(timeout=120)
    server.shutdown()

    if not received_code:
        print("Timed out waiting for authorization. Please try again.")
        return

    code = received_code[0]
    resp = httpx.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        },
    )
    if resp.status_code != 200:
        print(f"Token exchange failed: {resp.text}")
        return

    tokens = resp.json()
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        print("No refresh_token in response — make sure 'access_type=offline' and 'prompt=consent' are set.")
        print(f"Response: {tokens}")
        return

    print(f"\nSuccessfully obtained refresh token!")

    # Update .env file
    env_text = ENV_PATH.read_text() if ENV_PATH.exists() else ""
    lines = env_text.splitlines()
    updated = {}

    def set_env(key, value):
        for i, line in enumerate(lines):
            if line.startswith(f"{key}="):
                lines[i] = f"{key}={value}"
                updated[key] = True
                return
        lines.append(f"{key}={value}")
        updated[key] = True

    set_env("GMAIL_CLIENT_ID", client_id)
    set_env("GMAIL_CLIENT_SECRET", client_secret)
    set_env("GMAIL_REFRESH_TOKEN", refresh_token)

    ENV_PATH.write_text("\n".join(lines) + "\n")
    print(f"Updated {ENV_PATH} with GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN")
    print("\nNext steps:")
    print("  1. Reset the Anthropic apps: set status=pending_review in data/pipeline_state.json")
    print("  2. Re-run: PYTHONPATH=. python -m orchestration.runner --submit-only --auto-approve")


if __name__ == "__main__":
    main()
