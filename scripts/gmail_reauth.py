"""
Non-interactive Gmail re-auth: refreshes GMAIL_REFRESH_TOKEN in .env.

Reuses the existing GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET from settings (no stdin
prompts). Opens the Google consent page in the browser and catches the redirect on
localhost:8765. The human just signs in to the APPLICANT account (the applicant Gmail account)
and clicks Allow.

Run:
    python scripts/gmail_reauth.py
"""
from __future__ import annotations

import http.server
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.config import get_settings

ENV_PATH = Path(__file__).parent.parent / ".env"
SCOPES = "https://www.googleapis.com/auth/gmail.readonly"
REDIRECT_URI = "http://localhost:8765"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def _emit(m: str) -> None:
    print(m, flush=True)


def main() -> None:
    cfg = get_settings()
    client_id = (cfg.gmail_client_id or "").strip()
    client_secret = (cfg.gmail_client_secret or "").strip()
    if not client_id or not client_secret:
        _emit("<<ERROR>> GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET missing in .env"); return

    params = {
        "client_id": client_id, "redirect_uri": REDIRECT_URI, "response_type": "code",
        "scope": SCOPES, "access_type": "offline", "prompt": "consent",
    }
    auth_url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    received: list[str] = []
    stop = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" in q:
                received.append(q["code"][0])
                self.send_response(200); self.end_headers()
                self.wfile.write(b"<h2>Authorization successful. You can close this tab.</h2>")
                stop.set()
            else:
                self.send_response(400); self.end_headers()
                self.wfile.write(b"No code received.")
        def log_message(self, *a):  # silence
            pass

    server = http.server.HTTPServer(("localhost", 8765), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    _emit("<<AUTH_URL>> " + auth_url)
    _emit("<<WAITING>> Sign in as the applicant Gmail account and click Allow (up to 5 min)…")
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    stop.wait(timeout=300)
    server.shutdown()
    if not received:
        _emit("<<ERROR>> timed out waiting for Google consent"); return

    resp = httpx.post(TOKEN_URL, data={
        "code": received[0], "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": REDIRECT_URI, "grant_type": "authorization_code"}, timeout=20)
    if resp.status_code != 200:
        _emit(f"<<ERROR>> token exchange failed: {resp.text[:200]}"); return
    rt = resp.json().get("refresh_token")
    if not rt:
        _emit(f"<<ERROR>> no refresh_token in response: {resp.json()}"); return

    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    for i, ln in enumerate(lines):
        if ln.startswith("GMAIL_REFRESH_TOKEN="):
            lines[i] = f"GMAIL_REFRESH_TOKEN={rt}"; break
    else:
        lines.append(f"GMAIL_REFRESH_TOKEN={rt}")
    ENV_PATH.write_text("\n".join(lines) + "\n")
    _emit("<<OK>> GMAIL_REFRESH_TOKEN refreshed and written to .env")

    # Verify it works immediately
    tok = httpx.post(TOKEN_URL, data={
        "grant_type": "refresh_token", "refresh_token": rt,
        "client_id": client_id, "client_secret": client_secret}, timeout=20)
    if tok.status_code == 200 and tok.json().get("access_token"):
        at = tok.json()["access_token"]
        prof = httpx.get("https://gmail.googleapis.com/gmail/v1/users/me/profile",
                         headers={"Authorization": f"Bearer {at}"}, timeout=15).json()
        _emit(f"<<VERIFIED>> token works — inbox: {prof.get('emailAddress')}")
    else:
        _emit("<<WARN>> token written but refresh test failed")


if __name__ == "__main__":
    main()
