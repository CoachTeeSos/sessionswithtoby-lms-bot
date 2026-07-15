#!/usr/bin/env python3
"""Exchange an OAuth redirect URL/code for a token and save it.
Usage: .venv/bin/python gexchange.py "<full http://localhost/?code=... URL or just the code>"
Saves /data/google_token.json (auto-refreshing).
"""
import sys, json
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/contacts.readonly",
]
SECRET = "/data/google_client_secret.json"
VERIFIER = "/data/google_oauth_verifier.txt"
TOKEN = "/data/google_token.json"

def main():
    arg = sys.argv[1]
    code = arg.split("code=")[1].split("&")[0] if "code=" in arg else arg
    flow = InstalledAppFlow.from_client_secrets_file(SECRET, SCOPES)
    flow.redirect_uri = "http://localhost"
    flow.code_verifier = open(VERIFIER).read()
    flow.fetch_token(code=code)
    c = flow.credentials
    with open(TOKEN, "w") as f:
        json.dump({
            "token": c.token, "refresh_token": c.refresh_token,
            "token_uri": c.token_uri, "client_id": c.client_id,
            "client_secret": c.client_secret, "scopes": c.scopes,
        }, f)
    print("TOKEN SAVED. refresh_token present:", bool(c.refresh_token))

if __name__ == "__main__":
    main()
