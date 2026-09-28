#!/usr/bin/env python3
"""DIGITS DaaS endpoint audit — every surface the desktop app's renderer
bundle constructs that the base CLI never exercised.

Each function derives its exact request from the decompiled bundle
(beautified at /tmp/chunk_ab; line citations in AUDIT-FINDINGS.md), replays
it live with config.headers(), and archives the raw request/response pair
to audit_evidence/ (git-ignored).

Scope: voicemail service (flag/quota/greeting/transcript), caas contacts,
digitslink account linking, bulk contactCapabilities, RCS chat extras
(messages/status fake receipts, isComposing, pager_large_message_mode,
group chat, file transfer, mstore tampering), USSD shortcode, and the
orchestrator logout. The emergency chat surface is documented only —
NEVER invoked.
"""

import base64
import hashlib
import json
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests

import config
import digits_api

EVID_DIR = config.ROOT / "audit_evidence"
VM = config.VOICEMAIL                       # https://cpaas-geo.t-mobile.com/v1/daasvmsvc
ORCH = config.ORCHESTRATOR
CHAT = config.CHAT
DAAS = config.DAAS
# hardcoded in the app bundle next to every daasvmsvc call (chunk_ab 7922)
VM_CLIENT_ID = "RkdOw9xHHfMLGKaTHRbTuzExZYngWBxm"
APP_VERSION = "DesktopApp_27.0.0_NA_DIGITS_2.5.19_Production"

_seq = [0]


# ------------------------------------------------------------- evidence ----

def evidence(tag, method, url, headers, body, r):
    """Archive one raw request/response exchange (local only, git-ignored)."""
    EVID_DIR.mkdir(exist_ok=True)
    _seq[0] += 1
    now = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^a-z0-9_-]+", "-", tag.lower()).strip("-")
    p = EVID_DIR / f"{_seq[0]:03d}_{now}_{safe}.txt"
    lines = [
        f"=== {tag}  {datetime.now().isoformat()}",
        f"> {method} {url}",
        "> headers:",
    ]
    lines += [f"    {k}: {v}" for k, v in (headers or {}).items()]
    if body is not None:
        b = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
        lines += ["> body:", "    " + b[:20000]]
    lines += [f"< HTTP {r.status_code} {r.reason}", "< headers:"]
    lines += [f"    {k}: {v}" for k, v in r.headers.items()]
    txt = r.text if r.encoding or r.text.isprintable() else f"<{len(r.content)} bytes binary>"
    lines += ["< body:", "    " + txt[:20000]]
    p.write_text("\n".join(lines) + "\n")
    return p


def _record(tag, method, url, headers, body, r):
    evidence(tag, method, url, headers, body, r)
    txt = (r.text or "")[:300].replace("\n", " ")
    return r.status_code, txt


def _at():
    return digits_api.refresh()


def _sleep():
    time.sleep(1.0)  # backend is rate-sensitive — no parallel hammering


def _msisdn():
    return config.username()                      # 10-digit


def _line11():
    return f"1{config.username()}"                 # Ai() form: 1-prefixed


def _tel(n=None):
    return config.tel_uri(n or config.username())  # tel:+1XXXXXXXXXX


def _respond_to():
    env = config.load_env()
    return env.get("TMO_RESPOND_TO", "")


# ------------------------------------------------- chat session bootstrap ----

def _open_session(at, to):
    """Start a 1:1 chat session with `to` (the app's startOneToOneChat).
    Returns (session_id, terminator) — terminator is the `to` header value
    the app reuses on subsequent messages for the session."""
    body = {"chatSessionInformation": {
        "clientCorrelator": str(uuid.uuid4()),
        "originatorAddress": _tel(),
        "originatorName": config.display_name(),
        "subject": "",
        "tParticipantAddress": config.tel_uri(to),
        "tParticipantName": config.tel_uri(to)}}
    for attempt in range(3):
        body["chatSessionInformation"]["clientCorrelator"] = str(uuid.uuid4())
        r = requests.post(f"{CHAT}/sessions",
                          headers=config.headers(at, {"group": "false",
                                                      "to": to,
                                                      "x-p-associated-from": _tel(),
                                                      "Content-type": "application/json"}),
                          data=json.dumps(body), timeout=45)
        if r.status_code not in (222, 500):
            break
        time.sleep(4)
    evidence("chat-session-start", "POST", f"{CHAT}/sessions",
             config.headers(at), json.dumps(body), r)
    if r.status_code not in (200, 201):
        return None, None
    sid = (r.json().get("chatSessionInformation", {}).get("resourceURL", "")
           or "").rstrip("/").split("/")[-1]
    return sid, to


def _send_msg(at, sid, to, text):
    """chat/messages — the app's sendChatMessage (chunk_ab 11850). Returns
    the message's resourceURL tail (imdn id) or None."""
    msg = {"chatMessage": {
        "reportRequest": ["Sent", "Delivered", "Displayed", "Failed"],
        "text": text, "fromIMPU": _line11()}}
    r = requests.post(f"{CHAT}/messages",
                      headers=config.headers(at, {"conv-session-id": sid,
                                                  "to": to, "group": "false",
                                                  "x-p-associated-from": _tel(),
                                                  "Content-type": "application/json"}),
                      data=json.dumps(msg), timeout=45)
    mid = None
    if r.status_code in (200, 201, 202):
        try:
            mid = (r.json().get("chatMessage", {}).get("resourceURL", "")
                   or "").rstrip("/").split("/")[-1]
        except ValueError:
            pass
    evidence("chat-message-send", "POST", f"{CHAT}/messages",
             config.headers(at), json.dumps(msg), r)
    return mid, r


# ------------------------------------------------------------- voicemail ----

def vm_headers(at, extra=None):
    # chunk_ab 8459 formatPhoneNumbers: msisdn header is tel:+1XXXXXXXXXX
    h = {"client-id": VM_CLIENT_ID, "client-version": APP_VERSION,
         "msisdn": _tel()}
    if extra:
        h.update(extra)
    return config.headers(at, h)


def vm_list():
    """GET /voicemails (chunk_ab 7945-7953)."""
    at = _at()
    url = (f"{VM}/voicemails?audiotype=audio/mp4"
           f"&deltaSyncFromDate=2022-01-01T00:00:00.000Z&count=20&offset=0")
    r = requests.get(url, headers=vm_headers(at), timeout=45)
    st, txt = _record("vm-list", "GET", url, vm_headers(at), None, r)
    vms = []
    if r.status_code == 200:
        try:
            vms = r.json().get("voicemails", []) or []
        except ValueError:
            pass
    print(f"voicemails -> {st} ({len(vms)} entries)")
    for v in vms[:5]:
        print(f"  {v.get('objectid', '?')}  {v.get('date', '?')[:19]}  "
              f"{v.get('fromnumber') or v.get('returnnumber') or '?'}  "
              f"flag={v.get('flag', '?')}")
    if len(vms) > 5:
        print(f"  … {len(vms) - 5} more")
    return r


def vm_quota():
    """GET /voicemail/quota (chunk_ab 7970-7979)."""
    at = _at()
    url = f"{VM}/voicemail/quota"
    r = requests.get(url, headers=vm_headers(at), timeout=45)
    st, txt = _record("vm-quota", "GET", url, vm_headers(at), None, r)
    print(f"voicemail quota -> {st} {txt}")
    return r


def vm_greeting():
    """GET /voicemail/greeting — probe. The bundle has no such call (only
    DELETE /greeting/flag/{id} at 7981-7984 and an upload stub at 7985);
    greetings actually ride the /voicemails listing. Record what the
    backend says."""
    at = _at()
    url = f"{VM}/voicemail/greeting"
    r = requests.get(url, headers=vm_headers(at), timeout=45)
    st, txt = _record("vm-greeting-get", "GET", url, vm_headers(at), None, r)
    print(f"voicemail greeting GET -> {st} {txt}")
    return r


def vm_flag(objectid, seen=True):
    """PUT /voicemail/flag (chunk_ab 7919-7934) — mark own VM Seen."""
    at = _at()
    url = f"{VM}/voicemail/flag"
    body = json.dumps({"voicemails": [{"flag": "Seen" if seen else "Unseen",
                                       "objectid": objectid}]})
    h = vm_headers(at, {"Content-type": "application/json"})
    r = requests.put(url, headers=h, data=body, timeout=45)
    st, txt = _record(f"vm-flag-{'seen' if seen else 'unseen'}", "PUT", url,
                      h, body, r)
    print(f"voicemail flag -> {st} {txt}")
    return r


def vm_unflag(objectid):
    """DELETE /voicemail/flag/{id} (chunk_ab 7935-7944 + 7956-7959) — the
    app's mark-unseen path is a DELETE on the flag resource."""
    at = _at()
    url = f"{VM}/voicemail/flag/{objectid}"
    h = vm_headers(at)
    r = requests.delete(url, headers=h, timeout=45)
    st, txt = _record("vm-flag-delete", "DELETE", url, h, None, r)
    print(f"voicemail unflag -> {st} {txt}")
    return r


def vm_greeting_flag_delete(objectid):
    """DELETE /greeting/flag/{id} (chunk_ab 7981-7984) — reachability probe
    with a caller-supplied id; used for audit only."""
    at = _at()
    url = f"{VM}/greeting/flag/{objectid}"
    h = vm_headers(at)
    r = requests.delete(url, headers=h, timeout=45)
    st, txt = _record("vm-greeting-flag-delete", "DELETE", url, h, None, r)
    print(f"voicemail greeting flag delete -> {st} {txt}")
    return r


def vm_delete(objectid):
    """DELETE /voicemail/{id} (chunk_ab 7960-7969) — destructive; only ever
    point it at the caller's own voicemail id."""
    at = _at()
    url = f"{VM}/voicemail/{objectid}"
    h = vm_headers(at)
    r = requests.delete(url, headers=h, timeout=45)
    st, txt = _record("vm-delete", "DELETE", url, h, None, r)
    print(f"voicemail delete -> {st} {txt}")
    return r


def vm_transcript(objectid, language="en"):
    """PUT /voicemail/transcript (chunk_ab 7989-8002)."""
    at = _at()
    url = f"{VM}/voicemail/transcript"
    body = json.dumps({"language": language, "objectId": objectid})
    h = vm_headers(at, {"Content-type": "application/json"})
    r = requests.put(url, headers=h, data=body, timeout=45)
    st, txt = _record("vm-transcript", "PUT", url, h, body, r)
    print(f"voicemail transcript -> {st} {txt}")
    return r


# --------------------------------------------------------------- caas -------

def contacts_search(xdms=False):
    """GET /caas/contacts/search[Xdms] (chunk_ab 7520-7528). The plain
    search walks the DIGITS-native contact store; searchXdms walks contacts
    synced from a linked Google/Microsoft account."""
    at = _at()
    path = "/caas/contacts/searchXdms" if xdms else "/caas/contacts/search"
    url = f"{DAAS}{path}?pageNo=1&pageSize=200&all=true"
    h = config.headers(at)
    r = requests.get(url, headers=h, timeout=45)
    st, txt = _record(f"contacts-search{'-xdms' if xdms else ''}", "GET",
                      url, h, None, r)
    n = 0
    if r.status_code == 200:
        try:
            d = r.json()
            n = len(d.get("contacts", d if isinstance(d, list) else []))
        except ValueError:
            pass
    print(f"contacts {'xdms ' if xdms else ''}search -> {st} ({n} contacts) {txt[:120]}")
    return r


def contact_thumbnail(photo_id):
    """GET /caas/contacts/thumbnail (chunk_ab 7552-7556) — NOTE: the app
    puts the daasAccessToken in the QUERY STRING. Reachability probe with a
    caller-supplied photoId."""
    at = _at()
    url = (f"{DAAS}/caas/contacts/thumbnail?photoId={photo_id}"
           f"&daasAccessToken={at}&device_id={config.device()['device_uuid']}"
           "&client-type=cDigits")
    r = requests.get(url, timeout=45)
    st, txt = _record("contacts-thumbnail", "GET", url, {}, None, r)
    print(f"contact thumbnail -> {st} {txt[:120]}")
    return r


# ----------------------------------------------------------- digitslink -----

def linked_accounts():
    """GET /digitslink/v1/userlinkinfo (chunk_ab 7535-7540)."""
    at = _at()
    url = f"{DAAS}/digitslink/v1/userlinkinfo"
    h = config.headers(at)
    r = requests.get(url, headers=h, timeout=45)
    st, txt = _record("digitslink-userlinkinfo", "GET", url, h, None, r)
    print(f"userlinkinfo -> {st} {txt[:200]}")
    return r


def unlink_account(fed_type, fed_email):
    """POST /digitslink/api/unlink (chunk_ab 7541-7551) — form-encoded
    fed_type/fed_email. Unlinks a Google/Microsoft account from DIGITS."""
    at = _at()
    url = f"{DAAS}/digitslink/api/unlink"
    body = f"fed_type={fed_type}&fed_email={fed_email}"
    h = config.headers(at, {"Content-type": "application/x-www-form-urlencoded"})
    r = requests.post(url, headers=h, data=body, timeout=45)
    st, txt = _record("digitslink-unlink", "POST", url, h, body, r)
    print(f"unlink -> {st} {txt[:200]}")
    return r


# ------------------------------------------------------ capabilities -------

def capabilities_single(to):
    """GET /daas/contactCapabilities (chunk_ab 12042-12066) — single-line
    RCS capability discovery, headers isList=false + to."""
    at = _at()
    url = f"{ORCH}/daas/contactCapabilities"
    h = config.headers(at, {"isList": "false", "to": to,
                           "x-p-associated-from": _tel()})
    r = requests.get(url, headers=h, timeout=45)
    st, txt = _record("capabilities-single", "GET", url, h, None, r)
    print(f"capabilities[{to}] -> {st} {txt[:200]}")
    return r


def capabilities_bulk(numbers):
    """POST /daas/contactCapabilities with isList=true (chunk_ab
    12067-12084) — bulk RCS capability discovery for up to N numbers."""
    at = _at()
    url = f"{ORCH}/daas/contactCapabilities"
    body = json.dumps({"adhocContactList": {
        "contactId": [f"tel:1{config.normalize_msisdn(n)}" for n in numbers]}})
    h = config.headers(at, {"isList": "true", "x-p-associated-from": _tel(),
                            "Content-type": "application/json"})
    r = requests.post(url, headers=h, data=body, timeout=45)
    st, txt = _record("capabilities-bulk", "POST", url, h, body, r)
    print(f"capabilities bulk {numbers} -> {st} {txt[:300]}")
    return r


# -------------------------------------------------------------- USSD -------

def ussd(code):
    """POST /v1/digitsApi/shortcode (chunk_ab 9875-9909) — the USSD data
    channel. NOTE: in the app this rides an established USSD *call*
    session (startCall with isUSSD); posting it directly (no call leg)
    probes reachability of the endpoint without placing any call."""
    at = _at()
    url = f"{config.ORCHESTRATOR}/daas/call/shortcode"
    blob = ('<?xml version="1.0" encoding="UTF-8"?>'
            f'<ussd-data><language>en</language>'
            f'<ussd-string>{code}</ussd-string></ussd-data>')
    body = json.dumps({"ussdSessionInformation": {
        "originatorAddress": _tel(),
        "originatorName": config.display_name(),
        "destinationAddress": config.tel_uri(code) if re.match(r"^\+?\d+$", code) else code,
        "destinationName": code,
        "sdp": "",
        "ussdBlob": blob,
        "clientCorrelator": str(uuid.uuid4()),
        "resourceURL": ""}})
    h = config.headers(at, {"Content-type": "application/json"})
    r = requests.post(url, headers=h, data=body, timeout=45)
    st, txt = _record("ussd-shortcode", "POST", url, h, body, r)
    print(f"ussd {code} -> {st} {txt[:200]}")
    return r


# ---------------------------------------------------- large message mode ----

def send_large(to, text, subject=""):
    """POST /daas/chat/pager_large_message_mode (chunk_ab 12229-12269) —
    the app switches to imFormat IMLargeMode when bodyText > 1300 chars."""
    at = _at()
    to = config.normalize_msisdn(to)
    url = f"{CHAT}/pager_large_message_mode"
    big = len(text) > 1300
    body = json.dumps({"outboundMessageRequest": {
        "address": [f"tel:+1{to}"],
        "clientCorrelator": str(uuid.uuid4()),
        "outboundIMMessage": {
            "bodyText": text,
            "subject": subject,
            "imFormat": "IMLargeMode" if big else "IMPagerMode"},
        "receiptRequest": {"callbackData": "12345", "notifyURL": ""},
        "reportRequest": ["DeliveredToTerminal", "Displayed"],
        "senderAddress": _tel(),
        "senderName": _line11()}})
    h = config.headers(at, {"from": _line11(),
                            "mode": "large_msg" if big else "pager",
                            "to": to,
                            "x-p-associated-from": _tel(),
                            "Content-type": "application/json"})
    r = requests.post(url, headers=h, data=body, timeout=60)
    st, txt = _record("pager-large-message-mode", "POST", url, h, body, r)
    print(f"large-send ({len(text)} chars) -> {st} {txt[:200]}")
    return r


# ------------------------------------------------------------ receipts -----

def send_receipt(to, msg_id, status="Displayed", session_id=None):
    """POST /daas/chat/messages/status (chunk_ab 12194-12212) — posts an
    IMDN delivery/display receipt for a message. The senderAddress/
    receiverAddress are caller-controlled: a token holder can forge
    Delivered/Displayed receipts for arbitrary conversations."""
    at = _at()
    to = config.normalize_msisdn(to)
    if not session_id:
        session_id, _ = _open_session(at, to)
        if not session_id:
            print("[!] could not open chat session for receipt")
            return None
        _sleep()
    url = f"{CHAT}/messages/status"
    body = json.dumps({"messageStatusReport": {
        "status": status,
        "senderAddress": f"tel:+1{to}",
        "receiverAddress": _msisdn()}})
    h = config.headers(at, {"conv-session-id": session_id,
                            "group": "false",
                            "imdn-msg-id": msg_id,
                            "to": to,
                            "x-p-associated-from": _tel(),
                            "Content-type": "application/json"})
    r = requests.post(url, headers=h, data=body, timeout=45)
    st, txt = _record("messages-status-receipt", "POST", url, h, body, r)
    print(f"receipt {status} for {msg_id} -> {st} {txt[:200]}")
    return r


# ------------------------------------------------------------ composing -----

def send_composing(to, session_id=None):
    """POST /daas/chat/isComposing (chunk_ab 11966-11983) — typing
    indicator on an active session."""
    at = _at()
    to = config.normalize_msisdn(to)
    if not session_id:
        session_id, _ = _open_session(at, to)
        if not session_id:
            print("[!] could not open chat session for isComposing")
            return None
        _sleep()
    url = f"{CHAT}/isComposing"
    body = json.dumps({"isComposing": {"contenttype": "text/plain",
                                       "refresh": "7", "state": "active"}})
    h = config.headers(at, {"conv-session-id": session_id, "group": "false",
                            "x-p-associated-from": _tel(),
                            "Content-type": "application/json"})
    r = requests.post(url, headers=h, data=body, timeout=45)
    st, txt = _record("isComposing", "POST", url, h, body, r)
    print(f"isComposing -> {st} {txt[:200]}")
    return r


# ------------------------------------------------------------ group chat ----

def start_group_chat(participants, subject="cli group", is_closed=False):
    """POST /daas/chat/sessions group=true (chunk_ab 11740-11805) —
    startGroupChat."""
    at = _at()
    parts = []
    me = _tel()
    parts.append({"address": me, "isOriginator": True,
                 "name": config.display_name()})
    for p in participants:
        n = config.normalize_msisdn(p)
        if f"+1{n}" in me:
            continue
        parts.append({"address": f"tel:+1{n}", "isOriginator": False,
                      "name": f"+1{n}"})
    body = json.dumps({"groupChatSessionInformation": {
        "clientCorrelator": str(uuid.uuid4()),
        "isClosed": is_closed,
        "participant": parts,
        "subject": subject}})
    to_hdr = ",".join(p["address"] for p in parts if not p["isOriginator"])
    h = config.headers(at, {"group": "true", "to": to_hdr,
                            "x-p-associated-from": _tel(),
                            "Content-type": "application/json"})
    url = f"{CHAT}/sessions"
    r = requests.post(url, headers=h, data=body, timeout=45)
    st, txt = _record("groupchat-start", "POST", url, h, body, r)
    sid = None
    if r.status_code in (200, 201):
        try:
            sid = (r.json().get("groupChatSessionInformation", {})
                   .get("resourceURL", "") or "").rstrip("/").split("/")[-1]
        except ValueError:
            pass
    print(f"group chat start -> {st} session={sid} {txt[:150]}")
    return sid, r


def group_rejoin(session_id, participants, line=None):
    """POST /daas/groupchat/rejoin (chunk_ab 11984-12041) — re-admit a
    participant list to an existing group conversation."""
    at = _at()
    corr = str(uuid.uuid4())
    line = config.normalize_msisdn(line or config.username())
    plist = [{"address": f"1{line}", "name": config.display_name(),
              "clientCorrelator": corr}]
    for p in participants:
        n = config.normalize_msisdn(p)
        if n == line:
            continue
        plist.append({"address": f"1{n}", "name": f"1{n}",
                      "clientCorrelator": corr})
    body = json.dumps({"participantList": {"participant": plist}})
    h = config.headers(at, {"conv-session-id": session_id,
                            "x-p-associated-from": _tel(),
                            "Content-type": "application/json"})
    url = f"{ORCH}/daas/groupchat/rejoin"
    r = requests.post(url, headers=h, data=body, timeout=45)
    st, txt = _record("groupchat-rejoin", "POST", url, h, body, r)
    print(f"group chat rejoin -> {st} {txt[:200]}")
    return r


# --------------------------------------------------------- file transfer ----

def _multipart(json_payload, filename, data, mimetype):
    """Byte-for-byte rebuild of the bundle's pf() (chunk_ab 4523-4547)."""
    inner = uuid.uuid4().hex
    b = "===123456==="
    b64 = base64.b64encode(data).decode()
    j = json.dumps(json_payload)
    return (
        f"--{b}\r\n"
        "Content-Disposition: form-data; name=root-fields\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(j)}\r\n"
        "\r\n"
        f"{j}\r\n"
        "\r\n"
        f"--{b}\r\n"
        "Content-Disposition: form-data; name=attachments\r\n"
        f'Content-Type: multipart/mixed; boundary="{inner}"\r\n'
        "\r\n"
        f"--{inner}\r\n"
        f'Content-Type: {mimetype}; name="{filename}"\r\n'
        f'Content-Disposition: attachment; filename="{filename}"\r\n'
        "Content-Transfer-Encoding: base64\r\n"
        f"Content-Length: {len(b64)}\r\n"
        "\r\n"
        f"{b64}\r\n"
        "\r\n"
        f"--{inner}--\r\n"
        f"--{b}--"
    ).encode()


def send_file(to, path):
    """POST /daas/filetransfer/sessions (chunk_ab 11895-11965) — RCS file
    transfer. The outer content-type is text/plain with the real multipart
    type smuggled in Wrg-Content-Type; boundary literal ===123456===."""
    at = _at()
    to = config.normalize_msisdn(to)
    path = Path(path)
    data = path.read_bytes()
    recv = f"tel:+1{to}"
    payload = {"fileTransferSessionInformation": {
        "clientCorrelator": str(uuid.uuid4()),
        "fileInformation": {
            "fileDescription": "Local file",
            "fileDisposition": "Attachment",
            "fileSelector": {
                "hash": {"algorithm": "sha-1",
                         "value": hashlib.sha1(data).hexdigest()},
                "name": path.name,
                "size": len(data),
                "type": "text/plain"}},
        "originatorAddress": _tel(),
        "originatorName": config.display_name(),
        "receiverAddress": recv}}
    body = _multipart(payload, path.name, data, "text/plain")
    h = config.headers(at, {"accept": "application/json",
                            "content-type": "text/plain",
                            'Wrg-Content-Type':
                                'multipart/form-data; charset=utf-8; '
                                'boundary="===123456==="',
                            "to": recv, "group": "false",
                            "x-p-associated-from": _tel(),
                            "content-length": str(len(body))})
    url = f"{ORCH}/daas/filetransfer/sessions"
    r = requests.post(url, headers=h, data=body, timeout=120)
    st, txt = _record("filetransfer-sessions", "POST", url, h,
                      body[:2000], r)
    ft_id = None
    if r.status_code in (200, 201, 202):
        try:
            ru = (r.json().get("fileTransferSessionInformation", {})
                  .get("resourceURL", ""))
            ft_id = ru.rstrip("/").split("/")[-1]
        except ValueError:
            pass
    print(f"file transfer -> {st} ft_id={ft_id} {txt[:150]}")
    return ft_id, r


def download_file(resource_url, line=None, blob=True, accept="*/*"):
    """GET /daas/filetransfer/download (chunk_ab 12085-12112) — the app
    passes accept/blob/content-type/line/resourceUrl as HEADERS on a GET."""
    at = _at()
    h = config.headers(at, {"accept": accept,
                            "blob": "true" if blob else "false",
                            "content-type": "text/plain",
                            "line": config.normalize_msisdn(line or config.username()),
                            "resourceUrl": resource_url})
    url = f"{ORCH}/daas/filetransfer/download"
    r = requests.get(url, headers=h, timeout=60)
    st, txt = _record("filetransfer-download", "GET", url, h, None, r)
    print(f"file download -> {st} ({len(r.content)} bytes) {txt[:120]}")
    return r


# ------------------------------------------------------------- mstore -------

def mstore_bulk_update(object_ids, read=True, line=None):
    """POST /daas/mstore_objects/bulkUpdate (chunk_ab 12213-12224) — flip
    the read flag on arbitrary message-store object ids."""
    at = _at()
    ids = [i.split("_webgw_")[0] if "_webgw_" in i else i for i in object_ids]
    body = json.dumps({"objectIds": ids})
    h = config.headers(at, {"line": config.normalize_msisdn(line or config.username()),
                            "read": "true" if read else "false",
                            "Content-type": "application/json"})
    url = f"{ORCH}/daas/mstore_objects/bulkUpdate"
    r = requests.post(url, headers=h, data=body, timeout=45)
    st, txt = _record("mstore-bulkupdate", "POST", url, h, body, r)
    print(f"mstore bulkUpdate read={read} -> {st} {txt[:200]}")
    return r


def mstore_delete(object_ids, line=None):
    """POST /daas/mstore_objects/delete (chunk_ab 12179-12193) — delete
    message-store objects by id. Destructive: only ids of messages this
    CLI itself sent."""
    at = _at()
    ids = [i.split("_webgw_")[0] if "_webgw_" in i else i for i in object_ids]
    body = json.dumps({"objectIds": ids})
    h = config.headers(at, {"line": config.normalize_msisdn(line or config.username()),
                            "Content-type": "application/json"})
    url = f"{ORCH}/daas/mstore_objects/delete"
    r = requests.post(url, headers=h, data=body, timeout=45)
    st, txt = _record("mstore-delete", "POST", url, h, body, r)
    print(f"mstore delete -> {st} {txt[:200]}")
    return r


# -------------------------------------------------------------- logout ------

def daas_logout():
    """DELETE /digitsOrchestratorService/logout (chunk_ab 5955-5961) —
    tears down the DaaS registration for this device. Run `tmo register`
    afterwards to restore."""
    at = _at()
    url = f"{ORCH}/logout"
    h = config.headers(at, {"cdrInfo": "daasreg"})
    r = requests.delete(url, headers=h, timeout=45)
    st, txt = _record("orchestrator-logout", "DELETE", url, h, None, r)
    print(f"logout -> {st} {txt[:200]}")
    return r


# ------------------------------------------------------------ battery -------

def audit():
    """Read-only endpoint battery — GETs and discovery probes only, no
    writes, no logout, no emergency surface. Prints the results table."""
    rows = []

    def run(name, fn, *a, **k):
        try:
            r = fn(*a, **k)
            code = r.status_code if r is not None else "—"
            note = ""
            if r is not None and r.status_code == 200:
                try:
                    note = str(r.json())[:80]
                except ValueError:
                    note = r.text[:80]
        except Exception as e:  # noqa: BLE001 — battery must keep going
            code, note, r = "ERR", str(e)[:120], None
        rows.append((name, code, note))
        _sleep()

    run("GET  daasvmsvc/voicemails", vm_list)
    run("GET  daasvmsvc/voicemail/quota", vm_quota)
    run("GET  daasvmsvc/voicemail/greeting", vm_greeting)
    run("GET  orchestrator/caas/contacts/search", contacts_search)
    run("GET  orchestrator/caas/contacts/searchXdms",
        contacts_search, xdms=True)
    run("GET  orchestrator/digitslink/v1/userlinkinfo", linked_accounts)
    rt = _respond_to()
    if rt:
        run("GET  daas/contactCapabilities (single)",
            capabilities_single, rt)
        run("POST daas/contactCapabilities (isList bulk)",
            capabilities_bulk, [config.username(), rt])
        run("GET  daas/filetransfer/download (probe)",
            download_file, "probe-not-a-real-resource", blob=False)
    run("GET  orchestrator/caas/contacts/thumbnail (token in URL)",
        contact_thumbnail, "probe0")

    print()
    print(f"{'ENDPOINT':58} {'STATUS':>7}  RESULT")
    print("-" * 100)
    for name, code, note in rows:
        print(f"{name:58} {str(code):>7}  {note}")
    print()
    print(f"evidence: {_seq[0]} files in {EVID_DIR}/")
    return rows


# ---------------------------------------------------------------- cli -------

USAGE = """\
digits_audit — DaaS endpoint audit

commands:
  audit                          read-only battery + results table
  voicemail list|quota|greeting  voicemail service reads
  voicemail flag --id ID --seen|--unseen
  voicemail transcript --id ID [--lang en]
  voicemail delete --id ID       delete own voicemail (destructive)
  contacts [--xdms]              caas contact store search
  linked                         digitslink userlinkinfo
  capabilities --to N[,N...]     RCS capability discovery (bulk if >1 or --list)
  ussd --code CODE               POST /shortcode (no call leg)
  large-send --to N --text T     pager_large_message_mode (>1300 chars = large)
  file-send --to N --file PATH   filetransfer/sessions multipart
  file-download --url U          filetransfer/download probe
  receipt --to N --msg-id ID [--status Delivered|Displayed]
  composing --to N               isComposing typing indicator
  group --to N[,N...] [--subject S]     start group chat
  group-rejoin --session ID --to N[,N..]
  mstore update --id ID [--unread]      bulkUpdate read flag (own objects)
  mstore delete --id ID                 delete store object (own objects)
  logout                         DELETE /logout (deregisters — re-register after)
"""


def _opt(args, flag, default=None):
    return args[args.index(flag) + 1] if flag in args else default


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(USAGE)
        return
    cmd, rest = args[0], args[1:]

    if cmd == "audit":
        audit()
    elif cmd == "voicemail":
        sub = rest[0] if rest else "list"
        if sub == "list":
            vm_list()
        elif sub == "quota":
            vm_quota()
        elif sub == "greeting":
            vm_greeting()
        elif sub == "flag":
            oid = _opt(rest, "--id")
            if not oid:
                sys.exit("usage: voicemail flag --id OBJECTID --seen|--unseen")
            if "--unseen" in rest:
                vm_unflag(oid)
            else:
                vm_flag(oid, seen=True)
        elif sub == "transcript":
            oid = _opt(rest, "--id")
            if not oid:
                sys.exit("usage: voicemail transcript --id OBJECTID [--lang en]")
            vm_transcript(oid, _opt(rest, "--lang", "en"))
        elif sub == "delete":
            oid = _opt(rest, "--id")
            if not oid:
                sys.exit("usage: voicemail delete --id OBJECTID")
            vm_delete(oid)
        else:
            sys.exit(f"unknown voicemail subcommand: {sub}")
    elif cmd == "contacts":
        contacts_search(xdms="--xdms" in rest)
    elif cmd == "linked":
        linked_accounts()
    elif cmd == "capabilities":
        to = _opt(rest, "--to")
        if not to:
            sys.exit("usage: capabilities --to N[,N...] [--list]")
        nums = [n for n in to.split(",") if n]
        if len(nums) > 1 or "--list" in rest:
            capabilities_bulk(nums)
        else:
            capabilities_single(nums[0])
    elif cmd == "ussd":
        code = _opt(rest, "--code")
        if not code:
            sys.exit("usage: ussd --code '*#06#'")
        ussd(code)
    elif cmd == "large-send":
        to, text = _opt(rest, "--to"), _opt(rest, "--text")
        if not to or text is None:
            sys.exit("usage: large-send --to N --text T")
        send_large(to, text, _opt(rest, "--subject", ""))
    elif cmd == "file-send":
        to, path = _opt(rest, "--to"), _opt(rest, "--file")
        if not to or not path:
            sys.exit("usage: file-send --to N --file PATH")
        send_file(to, path)
    elif cmd == "file-download":
        url = _opt(rest, "--url")
        if not url:
            sys.exit("usage: file-download --url RESOURCEURL")
        download_file(url, blob="--text" not in rest)
    elif cmd == "receipt":
        to, mid = _opt(rest, "--to"), _opt(rest, "--msg-id")
        if not to or not mid:
            sys.exit("usage: receipt --to N --msg-id ID --status Delivered|Displayed")
        send_receipt(to, mid, _opt(rest, "--status", "Displayed"),
                     _opt(rest, "--session"))
    elif cmd == "composing":
        to = _opt(rest, "--to")
        if not to:
            sys.exit("usage: composing --to N")
        send_composing(to, _opt(rest, "--session"))
    elif cmd == "group":
        to = _opt(rest, "--to")
        if not to:
            sys.exit("usage: group --to N[,N...] [--subject S]")
        start_group_chat(to.split(","), _opt(rest, "--subject", "cli group"))
    elif cmd == "group-rejoin":
        sid, to = _opt(rest, "--session"), _opt(rest, "--to")
        if not sid or not to:
            sys.exit("usage: group-rejoin --session ID --to N[,N...]")
        group_rejoin(sid, to.split(","))
    elif cmd == "mstore":
        sub = rest[0] if rest else ""
        oid = _opt(rest, "--id")
        if not oid:
            sys.exit("usage: mstore update|delete --id ID [--unread]")
        if sub == "update":
            mstore_bulk_update([oid], read="--unread" not in rest)
        elif sub == "delete":
            mstore_delete([oid])
        else:
            sys.exit("usage: mstore update|delete --id ID")
    elif cmd == "logout":
        daas_logout()
    else:
        sys.exit(f"unknown command: {cmd}\n\n{USAGE}")


if __name__ == "__main__":
    main()
