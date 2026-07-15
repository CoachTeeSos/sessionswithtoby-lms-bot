#!/usr/bin/env python3
"""Sheets-only OAuth using the OFFICIAL Hermes PKCE pattern (verifier persisted to disk).
Scope narrowed to Sheets (non-sensitive) so Google's policy scanner lets it through.
redirect_uri matches what's registered on the web client: http://localhost
"""
import json, os, sys
from pathlib import Path
from google_auth_oauthlib.flow import Flow

SECRET = Path("/data/google_client_secret.json")
PENDING = Path("/data/google_oauth_pending.json")
TOKEN = Path("/data/google_token.json")
REDIRECT_URI = "http://localhost"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

def get_auth_url():
    flow = Flow.from_client_secrets_file(str(SECRET), scopes=SCOPES,
                                         redirect_uri=REDIRECT_URI,
                                         autogenerate_code_verifier=True)
    url, state = flow.authorization_url(access_type="offline", prompt="consent")
    PENDING.write_text(json.dumps({"state": state,
                                  "code_verifier": flow.code_verifier,
                                  "redirect_uri": REDIRECT_URI}, indent=2))
    print(url)

def exchange(code_or_url):
    if not PENDING.exists():
        print("ERROR: No pending session. Run get_auth_url first."); sys.exit(1)
    pending = json.loads(PENDING.read_text())
    code = code_or_url
    if code_or_url.startswith("http"):
        from urllib.parse import parse_qs, urlparse
        p = parse_qs(urlparse(code_or_url).query)
        code = p["code"][0]
        if p.get("state", [None])[0] != pending["state"]:
            print("ERROR: state mismatch"); sys.exit(1)
    flow = Flow.from_client_secrets_file(str(SECRET), scopes=SCOPES,
                                         redirect_uri=pending["redirect_uri"],
                                         state=pending["state"],
                                         code_verifier=pending["code_verifier"])
    flow.fetch_token(code=code)
    creds = flow.credentials
    payload = json.loads(creds.to_json())
    payload["scopes"] = list(creds.granted_scopes or SCOPES)
    TOKEN.write_text(json.dumps(payload, indent=2))
    PENDING.unlink(missing_ok=True)
    print(f"OK: Authenticated. Token -> {TOKEN}")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "exchange":
        exchange(sys.argv[2] if len(sys.argv) > 2 else "")
    else:
        get_auth_url()
