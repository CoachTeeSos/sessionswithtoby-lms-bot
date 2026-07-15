#!/usr/bin/env python3
"""Generate a Google OAuth consent URL (full Workspace scopes).
Usage: .venv/bin/python gauth.py
Requires /data/google_client_secret.json (Web-application type client).
Saves PKCE verifier to /data/google_oauth_verifier.txt for later exchange.
"""
import os
from google_auth_oauthlib.flow import InstalledAppFlow

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

def main():
    flow = InstalledAppFlow.from_client_secrets_file(SECRET, SCOPES)
    flow.redirect_uri = "http://localhost"
    url, _ = flow.authorization_url(prompt="consent", access_type="offline")
    open(VERIFIER, "w").write(flow.code_verifier)
    print("AUTH_URL=" + url)
    print("VERIFIER_LEN=" + str(len(flow.code_verifier)))

if __name__ == "__main__":
    main()
