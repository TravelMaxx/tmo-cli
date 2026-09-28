"""Shared config: env loading, device identity, DaaS headers, tokens.

Identity model (mirrors the desktop app):
  - .env            : credentials + preferences (never committed)
  - device.json     : per-install device identity (uuid + session number),
                      generated on first run — every install is a new "device",
                      just like each browser instance the app supports
  - tokens.json     : DaaS access/refresh pair from `tmo login`
"""

import json
import os
import re
import sys
import uuid
from pathlib import Path

# Credential files resolve to the CURRENT directory when writable (so a
# pip-installed tmo keeps its session in the project you run it from),
# falling back to the install/repo directory.
import os as _os

_INSTALL_ROOT = Path(__file__).resolve().parent.parent
ROOT = Path.cwd() if _os.access(Path.cwd(), _os.W_OK) else _INSTALL_ROOT
ENV_FILE = ROOT / ".env"
TOKENS_FILE = ROOT / "tokens.json"
DEVICE_FILE = ROOT / "device.json"

DAAS = "https://cpaas-geo.t-mobile.com/v1/digitsApi"
ORCHESTRATOR = f"{DAAS}/digitsOrchestratorService"
CHAT = f"{ORCHESTRATOR}/daas/chat"
CALL = f"{ORCHESTRATOR}/daas/call"
SYNC = f"{DAAS}/daasmstoresync"
VOICEMAIL = "https://cpaas-geo.t-mobile.com/v1/daasvmsvc"


def load_env() -> dict:
    """os.environ overlaid with .env (env vars win)."""
    env = dict(os.environ)
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip())
    return env


def normalize_msisdn(raw: str) -> str:
    """Accept 10-digit, 1+10, +1+10; return 10-digit."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        raise SystemExit(f"[!] '{raw}' is not a valid 10-digit US number")
    return digits


def tel_uri(number: str) -> str:
    """10-digit -> tel:+1XXXXXXXXXX (the app's formatPhoneNumbers)."""
    n = normalize_msisdn(number)
    return f"tel:+1{n}"


def username() -> str:
    """The DIGITS line's msisdn (TMOBILE_USERNAME)."""
    u = load_env().get("TMOBILE_USERNAME", "").strip()
    if not u:
        raise SystemExit("[!] TMOBILE_USERNAME not set (put your 10-digit number in .env)")
    return normalize_msisdn(u)


def display_name() -> str:
    return load_env().get("TMO_DISPLAY_NAME", "DIGITS User")


def device_identity() -> dict:
    """Load or create this install's device identity."""
    _df = ROOT / "device.json"
    if _df.exists():
        try:
            d = json.loads(DEVICE_FILE.read_text())
            if d.get("device_uuid") and d.get("session_number"):
                return d
        except (json.JSONDecodeError, KeyError):
            pass
    d = {
        "device_uuid": f"urn:uuid:{uuid.uuid4()}",
        "session_number": "01." + uuid.uuid4().hex[:12],
        "created_at": str(uuid.uuid1()),
    }
    _df.write_text(json.dumps(d, indent=2))
    return d


_DEVICE = None


def device() -> dict:
    global _DEVICE
    if _DEVICE is None:
        _DEVICE = device_identity()
    return _DEVICE


def headers(access_token: str, extra: dict = None) -> dict:
    """The full header set the app's daasFetchWorker sends on every request."""
    d = device()
    cid = str(uuid.uuid4())
    h = {
        "accept": "application/json, text/plain, */*",
        "daasAccessToken": access_token,
        "client-type": "cDigits",
        "daasClientId": "DAAS_WEB",
        "device_id": d["device_uuid"],
        "x-correlation-id": cid,
        "trxId": cid,
        "unique-session-number": d["session_number"],
        "X-Mav-Client-Version": "DesktopApp_27.0.0_NA_DIGITS_2.5.19_Production",
    }
    if extra:
        h.update(extra)
    return h


def load_tokens() -> dict:
    tf = ROOT / "tokens.json"
    if not tf.exists():
        raise SystemExit("[!] not logged in — run `tmo login` first")
    t = json.loads(tf.read_text())
    tokens = t.get("tokens", t)
    if not tokens.get("accessToken"):
        raise SystemExit("[!] tokens.json has no accessToken — run `tmo login`")
    return tokens


def save_tokens(tokens: dict):
    from datetime import datetime
    (ROOT / "tokens.json").write_text(json.dumps(
        {"captured_at": datetime.now().isoformat(), "tokens": tokens}, indent=2))


def access_token() -> str:
    return load_tokens()["accessToken"]
