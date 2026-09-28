#!/usr/bin/env python3
"""Unified tmo dispatcher — used by both the repo `tmo` wrapper and the
pip-installed `tmo` console script.

Runs the lib scripts as subprocesses (each gets lib/ on sys.path[0], so the
flat `import config` style works everywhere).
"""

import getpass
import os
import subprocess
import sys
from pathlib import Path

LIB = Path(__file__).resolve().parent

USAGE = """\
tmo — DIGITS CLI

commands:
  login [--username N]         browser login (password + SMS 2FA) -> tokens
  register                     activate this device on the line
  login                        browser login (the ONLY auth/refresh path)
  status                       token + device state
  sms send --to N --text T     send an SMS from the line
  threads                      conversation threads (device-scoped)
  calllogs                     call history (line-scoped)
  e911                         E911 address on file
  listen                       live notification monitor
  call --target N [--hold S]   place a voice call        [voice extra]
  ai-call --target N [--max S] AI voice agent (GPT-Live) [voice+ai extras]
  respond                      AI auto-responder for inbound texts [ai extra]

audit / endpoint surface:
  audit                        read-only endpoint battery + table
  voicemail list|quota|greeting|flag|transcript|delete
  contacts [--xdms]            caas contact store (searchXdms = linked acct)
  linked                       digitslink/v1/userlinkinfo
  capabilities --to N[,N..]    RCS capability discovery (bulk if list)
  ussd --code CODE             POST /shortcode USSD data channel
  large-send --to N --text T   >1300-char large-mode message
  file-send --to N --file P    RCS file transfer (multipart)
  file-download --url U        filetransfer/download probe
  receipt --to N --msg-id ID   forge IMDN Delivered/Displayed receipt
  composing --to N             isComposing typing indicator
  group --to N[,N..]           start group chat session
  group-rejoin --session ID --to N[,N..]
  mstore update|delete --id ID  message-store tampering (own objects only)
  logout                       DELETE /logout (deregisters device)
"""


def run(script, *args):
    sys.exit(subprocess.run([sys.executable, str(LIB / script), *args]).returncode)


def env_root() -> Path:
    """Directory holding .env/tokens: cwd if writable, else install dir."""
    cwd = Path.cwd()
    if os.access(cwd, os.W_OK):
        return cwd
    return LIB.parent


def cmd_login(args):
    username, password = None, None
    if "--username" in args:
        i = args.index("--username")
        if i + 1 < len(args):
            username = args[i + 1]
    root = env_root()
    env_file = root / ".env"

    existing = {}
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                existing[k.strip()] = v.strip()

    import re
    if username:
        d = re.sub(r"\D", "", username)
        if len(d) == 11 and d.startswith("1"):
            d = d[1:]
        if len(d) != 10:
            sys.exit("username must be a 10-digit number")
    else:
        username = existing.get("TMOBILE_USERNAME") or input("T-Mobile phone number: ").strip()
        d = re.sub(r"\D", "", username)
        if len(d) == 11 and d.startswith("1"):
            d = d[1:]
        if len(d) != 10:
            sys.exit("username must be a 10-digit number")

    password = existing.get("TMOBILE_PASSWORD") or getpass.getpass("Password: ")

    existing["TMOBILE_USERNAME"] = d
    existing["TMOBILE_PASSWORD"] = password
    env_file.write_text("\n".join(f"{k}={v}" for k, v in existing.items()) + "\n")
    os.chmod(env_file, 0o600)
    print(f"[*] credentials saved to {env_file}")
    run("digits_login.py")


def _print_help():
    """Prefer docs/help.md (full reference); fall back to built-in usage."""
    from pathlib import Path as _P
    doc = _P(__file__).resolve().parent.parent / "docs" / "help.md"
    if doc.exists():
        print(doc.read_text())
    else:
        print(USAGE)


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        _print_help()
        sys.exit(0)

    cmd, rest = args[0], args[1:]
    simple = {"register": ("digits_api.py", ["register"]),
             "status": ("digits_api.py", ["status"]),
             "threads": ("digits_api.py", ["threads"]),
             "calllogs": ("digits_api.py", ["calllogs"]),
             "listen": ("digits_ws_listener.py", []),
             "respond": ("digits_respond.py", []),
             "audit": ("digits_audit.py", ["audit"]),
             "linked": ("digits_audit.py", ["linked"]),
             "logout": ("digits_audit.py", ["logout"]),
             "group-rejoin": ("digits_audit.py", ["group-rejoin"]),
             "file-download": ("digits_audit.py", ["file-download"])}

    passthrough = {"voicemail", "ussd", "large-send", "file-send", "receipt",
                   "group", "mstore", "contacts", "capabilities", "composing"}



    if cmd == "login":
        cmd_login(rest)
    elif cmd == "sms":
        if not rest or rest[0] != "send":
            print("usage: tmo sms send --to <number> --text <text>")
            sys.exit(1)
        to = text = None
        r = rest[1:]
        if "--to" in r:
            to = r[r.index("--to") + 1]
        if "--text" in r:
            text = r[r.index("--text") + 1]
        if not to or text is None:
            print("usage: tmo sms send --to <number> --text <text>")
            sys.exit(1)
        run("digits_api.py", "send_sms", to, text)
    elif cmd == "call":
        target = rest[rest.index("--target") + 1] if "--target" in rest else None
        hold = rest[rest.index("--hold") + 1] if "--hold" in rest else "30"
        if not target:
            print("usage: tmo call --target <number> [--hold seconds]")
            sys.exit(1)
        run("digits_call.py", "--target", target, "--hold", hold)
    elif cmd == "ai-call":
        target = rest[rest.index("--target") + 1] if "--target" in rest else None
        mx = rest[rest.index("--max") + 1] if "--max" in rest else "240"
        if not target:
            print("usage: tmo ai-call --target <number> [--max seconds]")
            sys.exit(1)
        run("digits_gpt_call.py", "--target", target, "--max", mx)
    elif cmd in passthrough:
        run("digits_audit.py", cmd, *rest)
    elif cmd in simple:
        script, sargs = simple[cmd]
        run(script, *sargs)
    else:
        print(f"unknown command: {cmd}\n")
        print(USAGE)
        sys.exit(1)


if __name__ == "__main__":
    main()
