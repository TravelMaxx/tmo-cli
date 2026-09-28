#!/usr/bin/env python3
"""DIGITS DaaS API operations (the CLI workhorse).

  (auth)            tokens come ONLY from `tmo login` (browser flow) —
                     REST refresh is intentionally unsupported
  register           manageSession + register this device on the line
  send_sms TO TEXT   send an SMS from the line
  threads            list conversation threads (device-scoped)
  calllogs           line-scoped call history (numbers, times, duration)
  e911               E911 address on file for the line
  status             token + device state

Every command auto-refreshes when a refresh token is available, else falls
back to the existing access token (they live ~24h).
"""

import json
import sys
import time
import uuid

import requests

import config
import call_stack


def refresh():
    """Browser-login-only auth policy: NEVER rotate tokens via REST.

    The REST refreshToken endpoint is the bot-shaped path (scripted TLS,
    no browser fingerprint) that trips Akamai/fraud scoring. The ONLY way
    this CLI obtains credentials is `tmo login` — the real browser OAuth
    flow with the localhost:8080 callback. Access tokens live ~24h; when
    this one dies, the user runs `tmo login` again.
    """
    at = config.access_token()
    # cheap liveness probe — a plain authed GET
    r = requests.get(f"{config.ORCHESTRATOR}/daas/lines/deviceInstances",
                     headers=config.headers(at, {"msisdn": f"tel:+1{config.username()}"}),
                     timeout=30)
    if r.status_code == 401:
        raise SystemExit("[!] access token expired — run `tmo login` "
                         "(browser flow; the only supported refresh path)")
    print(f"token check -> {r.status_code} (access {at[:8]}... valid)")
    return at


def register():
    at = refresh()
    cu, st = call_stack.manage_session(at)
    print(f"manageSession -> {st}")
    # NOTE: do NOT connect the WS channel before registering — WRG 500s a
    # register that races a channel handshake (empirically established).
    # Register, then the channel (for listen/call) connects on demand.
    ok, msg = call_stack.register_line(at)
    print(f"register -> {msg}")


def send_sms(to, text):
    at = refresh()
    msisdn = config.username()
    to_tel = config.tel_uri(to)

    # session anchor: WRG requires the channel connected for chat sessions
    cu, _ = call_stack.manage_session(at)
    ch = None
    if cu:
        ch = call_stack.ChromeChannel()
        if not ch.start(cu):
            print("[!] WS channel failed — continuing (may 222)")
        else:
            time.sleep(1.5)  # let the session settle

    body = {"chatSessionInformation": {
        "clientCorrelator": str(uuid.uuid4()),
        "originatorAddress": f"tel:+1{msisdn}",
        "originatorName": config.display_name(),
        "subject": "",
        "tParticipantAddress": to_tel,
        "tParticipantName": to_tel}}
    for attempt in range(3):
        body["chatSessionInformation"]["clientCorrelator"] = str(uuid.uuid4())
        r = requests.post(f"{config.CHAT}/sessions",
                          headers=config.headers(at, {"group": "false", "to": to_tel[4:],
                                                      "x-p-associated-from": f"tel:+1{msisdn}",
                                                      "Content-type": "application/json"}),
                          data=json.dumps(body), timeout=45)
        print(f"chat/sessions try {attempt+1} -> {r.status_code}")
        if r.status_code not in (222, 500):
            break
        time.sleep(4)
    sid = None
    if r.status_code in (200, 201):
        sid = (r.json().get("chatSessionInformation", {}).get("resourceURL", "")
               or "").rstrip("/").split("/")[-1]
    if not sid:
        print(r.text[:200])
        return False
    time.sleep(2)

    msg = {"chatMessage": {
        "reportRequest": ["Sent", "Delivered", "Displayed", "Failed"],
        "text": text, "fromIMPU": f"tel:+1{msisdn}"}}
    r = requests.post(f"{config.CHAT}/messages",
                      headers=config.headers(at, {"conv-session-id": sid, "to": to_tel[4:],
                                                  "group": "false",
                                                  "x-p-associated-from": f"tel:+1{msisdn}",
                                                  "Content-type": "application/json"}),
                      data=json.dumps(msg), timeout=45)
    print(f"chat/messages -> {r.status_code}")
    if r.status_code in (200, 201, 202):
        print(f"[+] SMS sent to {to_tel}")
        ok = True
    else:
        print(r.text[:200])
        ok = False
    if ch:
        ch.stop()
    return ok


def threads():
    at = refresh()
    msisdn = config.username()
    r = requests.post(f"{config.SYNC}/syncthreads",
                      headers=config.headers(at, {"Content-type": "application/json"}),
                      data=json.dumps({"lines": [f"tel:+1{msisdn}"], "pageSize": 50}),
                      timeout=45)
    print(f"syncthreads -> {r.status_code}")
    print(r.text[:2000])


def calllogs():
    at = refresh()
    msisdn = config.username()
    r = requests.post(f"{config.SYNC}/synccalllogs",
                      headers=config.headers(at, {"Content-type": "application/json"}),
                      data=json.dumps({"lines": [f"tel:+1{msisdn}"], "pageSize": 50,
                                       "earliestDate": "2020-01-01T00:00:00Z"}),
                      timeout=45)
    print(f"synccalllogs -> {r.status_code}")
    try:
        j = r.json()
        logs = j.get("callLog", [])
        print(f"{len(logs)} entries")
        for c in logs[:50]:
            print(f"  {c.get('startTimestamp','?')} {c.get('direction','?'):3s} "
                  f"{c.get('to','?'):40s} {c.get('duration','?')}s {c.get('flags','')}")
    except Exception:
        print(r.text[:1500])


def e911():
    at = refresh()
    msisdn = config.username()
    r = requests.get(f"{config.ORCHESTRATOR}/daas/call/e911/information",
                     headers=config.headers(at, {"msisdn": msisdn, "clientId": "DIGITS_WEB"}),
                     timeout=45)
    print(f"e911 -> {r.status_code}")
    print(r.text[:500])


def status():
    tokens = config.load_tokens()
    d = config.device()
    print(f"device:   {d['device_uuid']}")
    print(f"session:  {d['session_number']}")
    print(f"line:     +1{config.username()}")
    print(f"access:   {tokens['accessToken'][:8]}...")
    print(f"refresh:  {'present' if tokens.get('refreshToken') else 'consumed/none'}")


def main():
    cmds = {"register": register, "threads": threads,
            "calllogs": calllogs, "e911": e911, "status": status}
    if len(sys.argv) < 2 or (sys.argv[1] not in cmds and sys.argv[1] != "send_sms"):
        print(__doc__)
        sys.exit(0)
    cmd = sys.argv[1]
    if cmd == "send_sms":
        if len(sys.argv) < 4:
            sys.exit("usage: digits_api.py send_sms <10-digit-to> <text>")
        send_sms(sys.argv[2], " ".join(sys.argv[3:]))
    else:
        cmds[cmd]()


if __name__ == "__main__":
    main()
