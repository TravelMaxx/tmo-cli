#!/usr/bin/env python3
"""Headless Playwright login — runs on any machine (or Daytona sandbox).

Drives the T-Mobile ID signin in headless Chromium: fills phone + password
from env (TMO_USERNAME / TMO_PASSWORD), waits for the SMS OTP on stdin,
types it, intercepts the localhost:8080 redirect, and exchanges the
authCode for DaaS tokens (tokens.json).

Stdout state machine (the orchestrating CLI polls it):
  STAGE:browser_up / page_loaded / phone_submitted / password_submitted
  STAGE:otp_prompt        <- waiting for OTP on stdin
  STAGE:otp_submitted / authcode <code> / tokens <summary> / done
  STAGE:error <message>
"""

import json
import os
import select
import sys
import time
import urllib.parse
import uuid

import requests
from playwright.sync_api import sync_playwright

DAAS = "https://cpaas-geo.t-mobile.com/v1/digitsApi"
ORCH = f"{DAAS}/digitsOrchestratorService"
EUI = "https://account.t-mobile.com"
CLIENT_ID = "DIGITS_WEB"
REDIRECT_URI = "http://localhost:8080"
SCOPE = ("associated_lines TMO_ID_profile token_validation permission "
         "SES_GENERIC_DEVICE_SERVICE_SCOPE SES_GENERIC_DEVICE_CONNECTIVITY_SCOPE openid")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

PHONE = os.environ.get("TMO_USERNAME", "")
PASSWORD = os.environ.get("TMO_PASSWORD", "")
HERE = os.path.dirname(os.path.abspath(__file__))
TOKENS_FILE = os.path.join(HERE, "tokens.json")


def stage(msg):
    print(f"STAGE:{msg}", flush=True)


def log(msg):
    print(f"      {msg}", flush=True)


def build_login_url():
    params = urllib.parse.urlencode({
        "client_id": CLIENT_ID, "scope": SCOPE, "response_type": "code",
        "access_type": "offline", "rtype": "rereg",
        "redirect_uri": REDIRECT_URI, "display": "o"})
    return f"{EUI}/signin/v2/?{params}"


def exchange_for_tokens(auth_code, device_uuid, session_number):
    cid = str(uuid.uuid4())
    headers = {
        "accept": "application/json, text/plain, */*",
        "authCode": auth_code, "cdrInfo": "daasreg",
        "Content-type": "application/json", "redirect-uri": REDIRECT_URI,
        "client-type": "cDigits", "daasClientId": "DAAS_WEB",
        "device_id": device_uuid, "unique-session-number": session_number,
        "x-correlation-id": cid, "trxId": cid,
        "X-Mav-Client-Version": "DesktopApp_27.0.0_NA_DIGITS_2.5.19_Production"}
    r = requests.post(f"{ORCH}/getToken", headers=headers, data="", timeout=45)
    log(f"getToken -> {r.status_code}")
    if r.status_code == 200:
        return r.json().get("data", r.json())
    raise RuntimeError(f"getToken failed {r.status_code}: {r.text[:300]}")


def wait_for_otp_stdin(timeout_s=300):
    stage("otp_prompt")
    log("waiting for OTP on stdin...")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r, _, _ = select.select([sys.stdin], [], [], 1.0)
        if r:
            line = sys.stdin.readline().strip()
            if line:
                return line
    raise RuntimeError("OTP timeout (300s)")


def main():
    if not PHONE or not PASSWORD:
        stage("error missing TMO_USERNAME/TMO_PASSWORD env")
        sys.exit(1)
    if not (len(PHONE) == 10 and PHONE.isdigit()):
        stage("error username must be a 10-digit number")
        sys.exit(1)

    device_uuid = f"urn:uuid:{uuid.uuid4()}"
    session_number = "01." + uuid.uuid4().hex[:12]

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=[
            "--disable-blink-features=AutomationControlled", "--no-sandbox"])
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900},
                                   locale="en-US")
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            "window.chrome={runtime:{}};"
            "Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});")
        page = ctx.new_page()

        captured = {"code": None}

        def catch_redirect(route):
            url = route.request.url
            params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            if "code" in params:
                captured["code"] = params["code"][0]
                stage(f"authcode {captured['code'][:12]}...")
                route.fulfill(status=200, content_type="text/html",
                              body="<h1>authcode captured</h1>")
            else:
                route.continue_()

        page.route("**localhost:8080**", catch_redirect)
        stage("browser_up")
        page.goto(build_login_url(), wait_until="domcontentloaded", timeout=60000)
        stage("page_loaded")

        try:
            s = page.wait_for_selector(
                "input[type='tel'], input[name*='phone'], input[id*='phone'], "
                "input[autocomplete='tel'], input[placeholder*='phone' i]",
                timeout=30000)
            s.fill(PHONE)
            page.keyboard.press("Enter")
            for sel in ("button[type='submit']", "button:has-text('Continue')",
                        "button:has-text('Next')"):
                try:
                    b = page.locator(sel).first
                    if b.is_visible(timeout=2000):
                        b.click()
                        break
                except Exception:
                    continue
            stage("phone_submitted")
        except Exception as e:
            stage(f"error phone step: {e}")
            page.screenshot(path=os.path.join(HERE, "login_fail_phone.png"))
            sys.exit(1)

        try:
            s = page.wait_for_selector("input[type='password']", timeout=30000)
            s.fill(PASSWORD)
            page.keyboard.press("Enter")
            for sel in ("button[type='submit']", "button:has-text('Sign in')",
                        "button:has-text('Log in')", "button:has-text('Continue')"):
                try:
                    b = page.locator(sel).first
                    if b.is_visible(timeout=2000):
                        b.click()
                        break
                except Exception:
                    continue
            stage("password_submitted")
        except Exception as e:
            stage(f"error password step: {e}")
            page.screenshot(path=os.path.join(HERE, "login_fail_password.png"))
            sys.exit(1)

        try:
            s = page.wait_for_selector(
                "input[autocomplete='one-time-code'], input[name*='otp' i], "
                "input[name*='code' i], input[id*='otp' i], input[id*='code' i], "
                "input[placeholder*='code' i]", timeout=45000)
            otp = wait_for_otp_stdin()
            s.fill(otp)
            page.keyboard.press("Enter")
            for sel in ("button[type='submit']", "button:has-text('Verify')",
                        "button:has-text('Continue')", "button:has-text('Submit')"):
                try:
                    b = page.locator(sel).first
                    if b.is_visible(timeout=2000):
                        b.click()
                        break
                except Exception:
                    continue
            stage("otp_submitted")
        except Exception as e:
            stage(f"error otp step: {e}")
            page.screenshot(path=os.path.join(HERE, "login_fail_otp.png"))
            sys.exit(1)

        deadline = time.time() + 60
        while not captured["code"] and time.time() < deadline:
            page.wait_for_timeout(500)
            if "code=" in page.url:
                params = urllib.parse.parse_qs(urllib.parse.urlparse(page.url).query)
                if "code" in params:
                    captured["code"] = params["code"][0]

        if not captured["code"]:
            stage("error no authCode redirect (60s)")
            page.screenshot(path=os.path.join(HERE, "login_fail_redirect.png"))
            sys.exit(1)

        try:
            tokens = exchange_for_tokens(captured["code"], device_uuid, session_number)
            with open(TOKENS_FILE, "w") as f:
                json.dump({"device_uuid": device_uuid,
                           "session_number": session_number,
                           "tokens": tokens}, f, indent=2)
            stage(f"tokens {json.dumps({k: str(v)[:8] for k in tokens})}")
        except Exception as e:
            stage(f"error token exchange: {e}")
            sys.exit(1)

        browser.close()
        stage("done")


if __name__ == "__main__":
    main()
