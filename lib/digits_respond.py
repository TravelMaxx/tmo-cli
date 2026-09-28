#!/usr/bin/env python3
"""GPT-powered auto-responder for inbound DIGITS texts.

Listens on the WRG notification channel. When a text arrives from the
allowlisted number (TMO_RESPOND_TO in .env — by design only ONE number,
everything else is logged and ignored), it is answered by OpenAI
gpt-5.4-mini running the mission prompt in lib/respond_persona.txt.

  inbound text ──► allowlist check ──► gpt-5.4-mini (mission + history)
                                          │
       reply ◄── chat/messages ◄───────────┘

Usage: tmo respond            (Ctrl+C to stop)
  .env: TMO_RESPOND_TO=<10-digit number you'll text FROM>
        OAI_API_KEY=<key>
"""

import json
import re
import sys
import time
import uuid
from pathlib import Path

import requests

import config
import call_stack

PERSONA_FILE = Path(__file__).resolve().parent / "respond_persona.txt"
MODEL = "gpt-5.4-mini"
SESSION_TTL = 150  # chat session reuse window (app: 185s inactivity)


def env_guard():
    env = config.load_env()
    respond_to = env.get("TMO_RESPOND_TO", "").strip()
    if not respond_to:
        raise SystemExit("[!] TMO_RESPOND_TO not set in .env — the single "
                         "number to auto-respond to (this is the safety rail)")
    if not env.get("OAI_API_KEY"):
        raise SystemExit("[!] OAI_API_KEY not set in .env")
    return config.normalize_msisdn(respond_to)


def norm_sender(addr) -> str:
    """Normalize any sender address (tel:+1..., sip:1...@..., bare digits)."""
    digits = re.sub(r"\D", "", str(addr or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def ask_gpt(mission: str, history: list, inbound: str) -> str:
    """One GPT-5.4-mini turn: mission + rolling history + the new text."""
    from openai import OpenAI
    client = OpenAI(api_key=config.load_env()["OAI_API_KEY"])
    convo = [{"role": "system", "content": mission}]
    for h in history[-12:]:  # rolling window
        convo.append({"role": "system", "content": f"{h['role']}: {h['text']}"})
    convo.append({"role": "user", "content": inbound})
    r = client.responses.create(model=MODEL, input=convo)
    return (r.output_text or "").strip()


def make_reply(at, msisdn, to_digits):
    """Create (or reuse) a 1:1 chat session; return a send function."""
    cache = getattr(make_reply, "_cache", None) or {}
    make_reply._cache = cache
    key = to_digits
    now = time.time()
    if key in cache and now - cache[key]["t"] < SESSION_TTL:
        return cache[key]["sid"]
    to_tel = f"tel:+1{to_digits}"
    body = {"chatSessionInformation": {
        "clientCorrelator": str(uuid.uuid4()),
        "originatorAddress": f"tel:+1{msisdn}",
        "originatorName": config.display_name(),
        "subject": "",
        "tParticipantAddress": to_tel, "tParticipantName": to_tel}}
    r = None
    for attempt in range(3):
        body["chatSessionInformation"]["clientCorrelator"] = str(uuid.uuid4())
        r = requests.post(f"{config.CHAT}/sessions",
                          headers=config.headers(at, {
                              "group": "false", "to": to_digits,
                              "x-p-associated-from": f"tel:+1{msisdn}",
                              "Content-type": "application/json"}),
                          data=json.dumps(body), timeout=45)
        if r.status_code not in (222, 500):
            break
        time.sleep(4)
    if r.status_code not in (200, 201):
        return None
    sid = (r.json().get("chatSessionInformation", {}).get("resourceURL", "")
           or "").rstrip("/").split("/")[-1]
    if not sid:
        return None
    cache[key] = {"sid": sid, "t": now}
    return sid


def send_reply(at, msisdn, to_digits, text, sid):
    r = requests.post(f"{config.CHAT}/messages",
                      headers=config.headers(at, {
                          "conv-session-id": sid, "to": to_digits,
                          "group": "false",
                          "x-p-associated-from": f"tel:+1{msisdn}",
                          "Content-type": "application/json"}),
                      data=json.dumps({"chatMessage": {
                          "reportRequest": ["Sent", "Delivered", "Displayed", "Failed"],
                          "text": text,
                          "fromIMPU": f"tel:+1{msisdn}"}}), timeout=45)
    return r.status_code in (200, 201, 202)


def main():
    respond_to = env_guard()
    at = config.access_token()
    msisdn = config.username()
    print(f"[*] auto-responder: line +1{msisdn} | replying ONLY to +1{respond_to}")
    print(f"[*] model: {MODEL} | mission: lib/respond_persona.txt")

    mission = PERSONA_FILE.read_text().strip()

    cu, st = call_stack.manage_session(at)
    print(f"[*] manageSession -> {st}")
    if not cu:
        raise SystemExit("[!] no channelUrl")
    ch = call_stack.ChromeChannel(log_file=str(config.ROOT / "respond.log"))
    if not ch.start(cu):
        raise SystemExit("[!] WS channel failed")

    # register before responding (the established order)
    ok, msg = call_stack.register_line(at)
    print(f"[*] register -> {msg}")

    histories = {}   # per-sender rolling history
    seen_ids = set() # dedupe inbound by imdn/message id

    print("\n[respond] LIVE — send a text from +1" + respond_to + ". Ctrl+C to stop.\n")
    try:
        while True:
            ch.new_msg.wait(timeout=1.0)
            ch.new_msg.clear()
            for raw in ch.drain():
                try:
                    content = json.loads(raw)
                except Exception:
                    continue
                n = content.get("chatMessageNotification") if isinstance(content, dict) else None
                if not n:
                    continue
                m = n.get("chatMessage", {})
                sender = norm_sender(m.get("senderAddress") or n.get("senderAddress"))
                text = str(m.get("text") or m.get("textcontent") or "")
                mid = str(m.get("imdnMessageID") or m.get("messageId") or uuid.uuid4())
                if not text or not sender or mid in seen_ids:
                    continue
                seen_ids.add(mid)

                ts = time.strftime("%H:%M:%S")
                if sender != respond_to:
                    print(f"[{ts}] (ignored — not allowlisted) from +1{sender}: {text[:80]}")
                    continue

                print(f"[{ts}] INBOUND from +1{sender}: {text}")

                # GPT reply
                try:
                    reply = ask_gpt(mission, histories.setdefault(sender, []), text)
                except Exception as e:
                    print(f"    [gpt] error: {e}")
                    continue
                if not reply:
                    continue
                histories[sender].append({"role": "them", "text": text})
                histories[sender].append({"role": "you", "text": reply})

                # send
                sid = make_reply(at, msisdn, sender)
                if sid and send_reply(at, msisdn, sender, reply, sid):
                    print(f"[{ts}] REPLIED: {reply[:120]}")
                else:
                    print(f"[{ts}] [!] send failed (session: {bool(sid)})")
    except KeyboardInterrupt:
        print("\n[respond] stopped")
    finally:
        ch.stop()


if __name__ == "__main__":
    main()
