#!/usr/bin/env python3
"""Live WRG notification listener.

Connects the notification channel (Chromium-held, full ping/ack protocol)
and prints every incoming event — texts, call notifications, registration
changes — as they happen on the line.

Usage: tmo listen
"""

import json
import time

import config
import call_stack


def main():
    at = config.access_token()
    cu, st = call_stack.manage_session(at)
    print(f"manageSession -> {st}")
    if not cu:
        raise SystemExit("[!] no channelUrl")
    log = config.ROOT / "notifications.log"
    ch = call_stack.ChromeChannel(log_file=str(log))
    if not ch.start(cu):
        raise SystemExit("[!] channel failed")
    print(f"[listen] live — logging to {log.name}. Ctrl+C to stop.\n")
    try:
        while True:
            ch.new_msg.wait(timeout=1.0)
            ch.new_msg.clear()
            for raw in ch.drain():
                ts = time.strftime("%H:%M:%S")
                try:
                    c = json.loads(raw)
                except Exception:
                    print(f"[{ts}] {str(raw)[:200]}")
                    continue
                if isinstance(c, dict):
                    if "chatMessageNotification" in c:
                        m = c["chatMessageNotification"].get("chatMessage", {})
                        print(f"[{ts}] TEXT from {m.get('senderAddress','?')}: "
                              f"{str(m.get('text',''))[:120]}")
                    elif "sessionStatusNotification" in c:
                        n = c["sessionStatusNotification"]
                        print(f"[{ts}] CALL {n.get('status','?')} "
                              f"rc={n.get('responseCode','?')}")
                    elif "sessionInvitationNotification" in c:
                        print(f"[{ts}] INCOMING CALL invitation")
                    else:
                        keys = list(c.keys())[:3]
                        print(f"[{ts}] {keys}")
                else:
                    print(f"[{ts}] {str(raw)[:150]}")
    except KeyboardInterrupt:
        print("\n[listen] stopped")
    finally:
        ch.stop()


if __name__ == "__main__":
    main()
