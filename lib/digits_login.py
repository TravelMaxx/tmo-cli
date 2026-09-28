#!/usr/bin/env python3
"""Local browser login: T-Mobile ID -> DaaS tokens.

Opens your default browser at the DIGITS OAuth signin. You complete
password + 2FA (SMS PIN). The redirect to http://localhost:8080 is captured
by a local listener, the authCode is exchanged for DaaS tokens, and they are
saved to tokens.json.

Run: tmo login
"""

import http.server
import json
import threading
import time
import urllib.parse
import uuid
import webbrowser
from datetime import datetime

import requests

import config

EUI = "https://account.t-mobile.com"
CLIENT_ID = "DIGITS_WEB"
REDIRECT_URI = "http://localhost:8080"
SCOPE = ("associated_lines TMO_ID_profile token_validation permission "
         "SES_GENERIC_DEVICE_SERVICE_SCOPE SES_GENERIC_DEVICE_CONNECTIVITY_SCOPE openid")

AUTHCODE_FILE = config.ROOT / "authcode.json"


def build_login_url():
    params = urllib.parse.urlencode({
        "client_id": CLIENT_ID, "scope": SCOPE, "response_type": "code",
        "access_type": "offline", "rtype": "rereg",
        "redirect_uri": REDIRECT_URI, "display": "o",
    })
    return f"{EUI}/signin/v2/?{params}"


class RedirectHandler(http.server.BaseHTTPRequestHandler):
    captured = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if "code" in params:
            RedirectHandler.captured = {
                "code": params["code"][0],
                "session_num": params.get("session_num", [None])[0],
                "userId": params.get("userId", [None])[0],
            }
            with open(AUTHCODE_FILE, "w") as f:
                json.dump({"captured_at": datetime.now().isoformat(),
                           **RedirectHandler.captured}, f, indent=2)
            body = b"<html><body><h1>Auth code captured! You can close this tab.</h1></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body><h1>Waiting for login...</h1></body></html>")

    def log_message(self, fmt, *args):
        pass


def exchange_for_tokens(auth_code):
    """POST authCode -> getToken (with this install's device identity)."""
    r = requests.post(
        f"{config.ORCHESTRATOR}/getToken",
        headers=config.headers("", {
            "authCode": auth_code,
            "cdrInfo": "daasreg",
            "Content-type": "application/json",
            "redirect-uri": REDIRECT_URI,
        }),
        data="", timeout=45)
    print(f"[*] getToken -> {r.status_code}")
    if r.status_code == 200:
        return r.json().get("data", r.json())
    print(f"[!] {r.text[:300]}")
    return None


def run_login():
    print("=" * 60)
    print("tmo login — T-Mobile ID (browser + 2FA)")
    print("=" * 60)

    server = http.server.HTTPServer(("127.0.0.1", 8080), RedirectHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[*] listening for OAuth redirect on {REDIRECT_URI}")

    url = build_login_url()
    print(f"[*] opening browser:\n    {url[:100]}...")
    webbrowser.open(url)

    print("[*] complete login in the browser (password + SMS 2FA)")
    timeout = 300
    start = datetime.now()
    while RedirectHandler.captured is None:
        if (datetime.now() - start).total_seconds() > timeout:
            print("[!] timed out waiting for login")
            server.shutdown()
            return None
        time.sleep(0.5)
    server.shutdown()

    code = RedirectHandler.captured["code"]
    tokens = exchange_for_tokens(code)
    if isinstance(tokens, dict) and tokens.get("accessToken"):
        config.save_tokens(tokens)
        print(f"[+] tokens saved to {config.TOKENS_FILE}")
        print("[+] run `tmo register` to activate this device on your line")
        return tokens
    print(f"[!] exchange failed — authCode preserved in {AUTHCODE_FILE}")
    return None


if __name__ == "__main__":
    run_login()
