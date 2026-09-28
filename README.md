# tmo-cli

Headless CLI client for **DIGITS from T-Mobile** — authenticate, send
texts, read call history, place voice calls, and run an AI voice agent on
a real phone line, all without the desktop app.

Built from a full protocol reverse-engineering of the DIGITS desktop app
(Electron): OAuth login flow, DaaS REST API, the WRG WebSocket
notification protocol (ping/ack keepalive), and the WebRTC/SDP call
signaling state machine.

> ⚠️ **Security research tool.** The DIGITS API accepts a single
> `daasAccessToken` header with no client attestation, and the desktop app
> stores that token unencrypted — meaning any process on the machine can
> operate the line. This tool demonstrates that exposure. Use only on your
> own account/line.

## Install

**Option A — pip (from GitHub):**
```bash
pip install git+https://github.com/TravelMaxx/tmo-cli.git
tmo --help
```

**Option B — one-line curl:**
```bash
curl -fsSL https://raw.githubusercontent.com/TravelMaxx/tmo-cli/main/quick-install.sh | bash
```

**Option C — clone + install script (development):**
```bash
git clone https://github.com/TravelMaxx/tmo-cli.git
cd tmo-cli
./install.sh
```

`install.sh` creates the venv, installs dependencies, downloads Chromium
(for the notification channel — see Architecture), and creates `.env`
from the template.

Then:

```bash
# 1. put your credentials in .env
$EDITOR .env            # TMOBILE_USERNAME=5551234567, TMOBILE_PASSWORD=...

# 2. login (opens your browser; complete password + SMS 2FA)
./tmo login

# 3. activate this device on your line
./tmo register

# 4. verify
./tmo status
```

## Usage

```bash
# SMS
./tmo sms send --to 5551234567 --text "hello from the CLI"

# Line-scoped call history (numbers, direction, duration, timestamps)
./tmo calllogs

# Conversation threads (device-scoped — see Notes)
./tmo threads

# E911 address on file for the line
./tmo e911

# Live monitor: print every incoming text/call notification
./tmo listen

# Place a voice call (rings a real phone)
./tmo call --target 5551234567 --hold 30

# AI voice agent on the call (GPT-Live; needs OAI_API_KEY in .env)
# customize the agent in lib/persona.txt first
./tmo ai-call --target 5551234567 --max 240
```

## Commands

| Command | What it does |
|---|---|
| `login` | Browser OAuth login (2FA) → `tokens.json`. `--username` flag for headless cred entry |
| `register` | `manageSession` + register this device on the line |
| `refresh` | Rotate the token pair |
| `status` | Token + device state |
| `sms send --to N --text T` | Send an SMS (full session chain) |
| `threads` | List conversation threads |
| `calllogs` | Full call history |
| `e911` | E911 address on file |
| `listen` | Live notification stream monitor |
| `call --target N` | Place a voice call (WebRTC/PCMU/opus) |
| `ai-call --target N` | AI voice agent holds the call (OpenAI GPT-Live) |
| `respond` | AI auto-replies to inbound texts from the allowlisted number (gpt-5.4-mini) |
| `audit` | Read-only endpoint battery + results table (voicemail, contacts, capabilities, digitslink, filetransfer probe) |
| `voicemail list\|quota\|greeting` | Voicemail mailbox, storage quota, greeting probe |
| `voicemail flag --id ID --seen\|--unseen` | Mark own voicemail read/unread |
| `voicemail transcript --id ID` | Queue server-side transcription |
| `voicemail delete --id ID` | Delete a voicemail (destructive) |
| `contacts [--xdms]` | caas contact store (`--xdms` = linked Google/Microsoft contacts) |
| `linked` | digitslink linked-account enumeration |
| `capabilities --to N[,N..]` | RCS capability discovery (single or bulk list) |
| `ussd --code CODE` | USSD data channel (`/daas/call/shortcode`) |
| `large-send --to N --text T` | >1300-char message via `pager_large_message_mode` |
| `file-send --to N --file P` | RCS file transfer (multipart file push) |
| `file-download --url U` | filetransfer/download probe |
| `receipt --to N --msg-id ID` | Post IMDN Delivered/Displayed receipt |
| `composing --to N` | Typing indicator |
| `group --to N[,N..]` | Start group chat session |
| `group-rejoin --session ID` | Rejoin existing group chat |
| `mstore update\|delete --id ID` | Message-store read-flag/delete (own objects) |
| `logout` | Deregister device (`DELETE /logout`) |

See **AUDIT-FINDINGS.md** for the per-endpoint security audit these
commands embody (request shapes, live results, decompiled-source
citations).

## Architecture

```
                          ┌──────────────────────────────────┐
  tmo CLI ── login ──►    │ account.t-mobile.com (browser)   │
      │                   │   phone + password + SMS 2FA     │
      │  authCode         └──────────────────────────────────┘
      ▼
  POST /getToken ──►  DaaS tokens (access / refresh / id)
      │
      ▼
  REST  : cpaas-geo.t-mobile.com/v1/digitsApi  (chat, call, sync, register)
  WS    : wrgcore.cnf.*.sip.t-mobile.com  — notification channel
  WebRTC: aiortc ⇄ WRG media server (PCMU/opus, DTLS-SRTP)
```

Key reverse-engineered details the implementation depends on:

- **Headers**: every request needs `daasAccessToken` + `device_id` +
  `unique-session-number` + `client-type: cDigits` + `daasClientId: DAAS_WEB`
- **WS channel protocol**: on connect send `PING Y:M:D:h:m:s` (repeat
  every 30s); ack every server message with `{"nsAck":{"msgId":…}}` — miss
  either and WRG drops your session (410s)
- **Call state machine**: no REST polling — Proceeding/183 and
  Connected/200 notifications arrive over WS and are answered by POSTing
  processed SDP back to `/call/status`
- **SDP**: WRG's SIP stack rejects non-Chrome-shaped offers; the offer is
  rebuilt in Chrome's exact format (single fingerprint, clean candidates)
- **TLS fingerprinting**: the WRG websocket nodes JA3-block python TLS —
  the channel is held by real headless Chromium (Playwright)
- **Async REST**: the API answers many POSTs with `202` + a `statusUrl`
  SSE stream that carries the real result

## Notes

- **Messages vs call history**: call history is line-scoped (full
  history from the carrier's store); SMS threads are device-scoped (a new
  device only sees messages from when it connected). `tmo listen` captures
  everything live.
- **RCS**: the chat API is an RCS/IMS stack — delivery receipts
  ("Displayed") work RCS-to-RCS; file transfer and group chat endpoints
  exist on the same token. Cross-carrier/iPhone recipients fall back to SMS.
- **Beyond SMS**: the same token drives voicemail (list/transcribe/
  delete), the caas contact store, RCS capability discovery, USSD codes
  (`/daas/call/shortcode`), >1300-char large-mode messages, RCS file
  transfer, group chat, IMDN receipts, and the message store — all
  mapped in AUDIT-FINDINGS.md.
- **Token lifetime**: access tokens live ~24h; refresh rotates on each
  use (single-use). `tmo login` re-mints when needed.
- **Fraud throttling**: rapid automated call attempts can trigger the
  carrier's fraud engine (`403 CC_TRANS_O_FRAUDULENT_MOBILE`). If calls
  suddenly fail with 403, stop and retry after 24–48h.

## Files

| Path | Purpose |
|---|---|
| `tmo` / `tmo.py` | CLI entry + dispatcher |
| `lib/config.py` | env, device identity, headers, tokens |
| `lib/call_stack.py` | WRG Chromium channel, G.711 codec, SDP shaper |
| `lib/digits_login.py` | browser OAuth login |
| `lib/digits_api.py` | refresh/register/sms/threads/calllogs/e911 |
| `lib/digits_call.py` | voice call client (full state machine) |
| `lib/digits_gpt_call.py` | GPT-Live AI voice bridge |
| `lib/digits_ws_listener.py` | live notification monitor |
| `lib/digits_audit.py` | endpoint-audit surfaces (voicemail, contacts, capabilities, USSD, large-mode, file transfer, receipts, group chat, mstore, logout) |
| `lib/persona.txt` | AI agent persona (edit per task) |
| `sandbox/tmo_login.py` | headless Playwright login (stdin OTP relay) |

## License

MIT — see LICENSE.
