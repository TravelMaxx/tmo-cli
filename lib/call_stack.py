"""Shared voice-call stack: WRG channel (Chromium), G.711 codec, SDP shaping.

Why Chromium for the WS channel: T-Mobile's edge JA3-filters python's TLS
ClientHello on the WRG websocket nodes; a real Chrome fingerprint passes.
The channel also speaks the app's exact protocol (decompiled from the
desktop bundle):
  - send "PING Y:M:D:h:m:s" on open, then every 30s
  - ack every server nsMsg with {"nsAck":{"msgId":...}} or the session dies
  - notifications live inside nsMsg.content.*
"""

import asyncio
import json
import re
import os
import queue
import socket
import threading
import time
from datetime import datetime

import requests

import config

# IPv4-only by default: many home networks blackhole IPv6 to WRG nodes.
_orig_getaddrinfo = socket.getaddrinfo
socket.getaddrinfo = lambda *a, **k: [r for r in _orig_getaddrinfo(*a, **k)
                                      if r[0] == socket.AF_INET]


def sse_poll(status_url, access_token, timeout=30):
    """Drain a 202 statusUrl SSE stream -> list of inner data dicts."""
    out = []
    try:
        r = requests.get(status_url, headers=config.headers(access_token),
                         timeout=timeout, stream=True)
        for line in r.iter_lines(decode_unicode=True):
            if line and line.startswith("data:"):
                try:
                    j = json.loads(line[5:].strip())
                    if "data" in j:
                        out.append(json.loads(j["data"]))
                    elif j.get("name") == "COMPLETED_STREAM":
                        break
                except (json.JSONDecodeError, KeyError):
                    continue
    except Exception:
        pass
    return out


def manage_session(access_token, tries=4):
    """manageSession -> channelUrl (retries; SSE service is flaky)."""
    url = (f"{config.ORCHESTRATOR}/manageSession?chat=true&voip=true&ft=true"
           "&video=true&racm=true&standalonemessaging=true&ussd=true&nms=true&stMsg=true")
    for attempt in range(tries):
        r = requests.get(url, headers=config.headers(access_token, {"cdrInfo": "daasreg"}),
                         timeout=30)
        if r.status_code == 202:
            time.sleep(1.5)
            for inner in sse_poll(r.json().get("statusUrl", ""), access_token):
                if inner.get("channelUrl"):
                    return inner["channelUrl"], inner.get("status", "")
        elif r.status_code == 200:
            j = r.json()
            cu = j.get("channelUrl") or j.get("data", {}).get("channelUrl")
            if cu:
                return cu, j.get("status", "")
        time.sleep(3)
    return None, f"HTTP failures after {tries} tries"


def register_line(access_token):
    """linesAndDeviceInfo + register (the app's fullAuthRegSequence tail).

    friendlyName MUST be the line's real name from the account (the app uses
    i.lineName from linesAndDeviceInfo) — a mismatched name makes WRG's
    register throw a 500.
    """
    msisdn = config.username()
    device_name = config.load_env().get("TMO_DEVICE_NAME", "tmo-cli")

    # fetch account lines to learn the real line name (async 202 pattern)
    line_name = msisdn
    r0 = requests.post(
        f"{config.ORCHESTRATOR}/linesAndDeviceInfo",
        headers=config.headers(access_token, {"config": "false", "tmoUserId": "true",
                                              "unusedLines": "true",
                                              "Content-type": "application/json"}),
        data=json.dumps({"applicationCategory": "webdigits",
                         "deviceModel": "Group-Production-cDIGITS-WebDIGITS",
                         "deviceName": device_name}),
        timeout=30)
    def _norm(n):
        d = re.sub(r"\D", "", str(n or ""))
        return d[1:] if len(d) == 11 and d.startswith("1") else d

    if r0.status_code == 202:
        time.sleep(1.5)
        for inner in sse_poll(r0.json().get("statusUrl", ""), access_token):
            for l in inner.get("lines", []):
                # account returns msisdn as 11-digit (1-prefixed) — normalize
                if _norm(l.get("msisdn")) == msisdn and l.get("lineName"):
                    line_name = l["lineName"]
                    break
    body = {
        "applicationCategory": "webdigits",
        "deviceModel": "Group-Production-cDIGITS-WebDIGITS",
        "deviceName": device_name, "deviceOsType": 0, "deviceType": 33,
        # WRG requires the 1-prefixed (11-digit) msisdn here — the 10-digit
        # form 500s (the account APIs return 11-digit; the app passes through)
        "serviceItems": [{"serviceName": "vowifi", "msisdn": f"1{msisdn}",
                          "isSim": False, "friendlyName": line_name}],
        "wrgPushToken": "someRandomStringNeededByWRG", "wrgServiceName": "wrg",
        "ottSimAndPnsInfo": {"appCapabilityTags": [], "clientMode": "DataMode",
                             "pnsEnabled": False, "pnsExtendedCapability": False,
                             "retryCallNotifications": False, "simLine": None},
    }
    for attempt in range(3):
        r = requests.post(
            f"{config.ORCHESTRATOR}/register",
            headers=config.headers(access_token, {"cdrInfo": "daasreg", "is-upgrade": "false",
                                                  "config": "false", "modeSwitch": "false",
                                                  "unusedLines": "true",
                                                  "Content-type": "application/json"}),
            data=json.dumps(body), timeout=30)
        if r.status_code not in (222, 500):
            break
        time.sleep(4)  # SESSION_RECOVERY_IN_PROGRESS — the app retries this
    if r.status_code == 202:
        time.sleep(2)
        for inner in sse_poll(r.json().get("statusUrl", ""), access_token):
            if any(l.get("regStatus") == "1" for l in inner.get("lines", [])):
                return True, "REGISTERED"
        return False, "202 but no regStatus=1"
    ok = r.status_code == 200
    return ok, ("REGISTERED" if ok else r.text[:120])


class ChromeChannel:
    """WRG notification channel held by headless Chromium (real Chrome TLS).

    Implements the app's exact WS protocol (ping keepalive + nsAck for
    every message). Parsed nsMsg.content objects are queued for consumers.
    """

    def __init__(self, log_file=None):
        self.connected = threading.Event()
        self.stop_flag = threading.Event()
        self.new_msg = threading.Event()
        self.q = queue.Queue()
        self.t = None
        self.log_file = log_file

    def start(self, url):
        self.t = threading.Thread(target=self._run, args=(url,), daemon=True)
        self.t.start()
        return self.connected.wait(timeout=15)

    def _run(self, url):
        from playwright.sync_api import sync_playwright
        try:
            pw = sync_playwright().start()
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page()
            page.add_init_script("""
                window.__notes = []; window.__wsOpen = false; window.__wsClose = null;
                (function() {
                    const o = window.WebSocket;
                    window.WebSocket = function(u, p) {
                        const ws = p ? new o(u, p) : new o(u);
                        ws.addEventListener('open', () => {
                            window.__wsOpen = true;
                            const ping = () => {
                                const d = new Date();
                                ws.send('PING ' + d.getFullYear() + ':' + (d.getMonth()+1)
                                  + ':' + d.getDate() + ':' + d.getHours() + ':' + d.getMinutes()
                                  + ':' + d.getSeconds());
                            };
                            ping(); setInterval(ping, 30000);
                        });
                        ws.addEventListener('message', (ev) => {
                            const data = ev.data;
                            if (typeof data === 'string' && data.indexOf('PONG') === 0) return;
                            try {
                                const j = JSON.parse(data);
                                const ns = j.nsMsg || (j.data && j.data.nsMsg);
                                if (ns && ns.msgId) {
                                    ws.send(JSON.stringify({nsAck: {msgId: ns.msgId}}));
                                    if (ns.content) window.__notes.push(JSON.stringify(ns.content));
                                    return;
                                }
                                window.__notes.push(JSON.stringify(j));
                                return;
                            } catch (e) {}
                            window.__notes.push(String(data));
                        });
                        ws.addEventListener('close', (ev) => { window.__wsClose = ev.code; });
                        return ws;
                    };
                    window.WebSocket.prototype = o.prototype;
                })();
                new window.WebSocket(WSURL);
            """.replace("WSURL", json.dumps(url)))
            page.goto("about:blank")
            for _ in range(50):
                if self.stop_flag.is_set():
                    break
                try:
                    if page.evaluate("window.__wsOpen"):
                        print("      [ws] CONNECTED (Chrome TLS, ping+ack protocol)")
                        self.connected.set()
                        break
                except Exception:
                    pass
                time.sleep(0.3)
            while not self.stop_flag.is_set():
                try:
                    if page.evaluate("window.__wsClose") is not None:
                        print(f"      [ws] closed code={page.evaluate('window.__wsClose')}")
                        break
                    for f in (page.evaluate("window.__notes.splice(0, window.__notes.length)") or []):
                        self.q.put(f)
                        if self.log_file:
                            with open(self.log_file, "a") as fh:
                                fh.write(f"{datetime.now().isoformat()} {f}\n")
                except Exception:
                    break
                time.sleep(0.2)
            try:
                browser.close()
                pw.stop()
            except Exception:
                pass
        except Exception as e:
            print(f"      [ws] thread err {e}")

    def drain(self):
        out = []
        try:
            while True:
                out.append(self.q.get_nowait())
        except queue.Empty:
            pass
        if out:
            self.new_msg.set()
        return out

    def stop(self):
        self.stop_flag.set()
        if self.t:
            self.t.join(timeout=5)


# ------------------------------------------------ G.711 u-law (audioop-free) --

_BIAS = 0x84
_QUANT_MASK = 0xF
_SEG_MASK = 0x70
_SEG_SHIFT = 4
_SIGN_BIT = 0x80
_SEG_UEND = (0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF)

_ULAW_DECODE = [0] * 256


def _init_ulaw_decode():
    for b in range(256):
        u = ~b & 0xFF
        t = ((u & _QUANT_MASK) << 3) + _BIAS
        t <<= (u & _SEG_MASK) >> _SEG_SHIFT
        _ULAW_DECODE[b] = (_BIAS - t) if (u & _SIGN_BIT) else (t - _BIAS)


_init_ulaw_decode()


def _linear_to_ulaw(s16: int) -> int:
    v = s16 >> 2
    if v < 0:
        v = -v
        mask = 0x7F
    else:
        mask = 0xFF
    if v > 8159:
        v = 8159
    v += _BIAS >> 2
    seg = 8
    for i in range(8):
        if v <= _SEG_UEND[i]:
            seg = i
            break
    if seg >= 8:
        return 0x7F ^ mask
    uval = (v >> (seg + 1)) & _QUANT_MASK
    return ((seg << 4) | uval) ^ mask


_ULAW_ENCODE = bytes(_linear_to_ulaw(s) for s in range(-32768, 32768))


def pcm_to_ulaw(pcm: bytes) -> bytes:
    enc = _ULAW_ENCODE
    n = len(pcm) // 2
    out = bytearray(n)
    for j in range(n):
        u = pcm[2 * j] | (pcm[2 * j + 1] << 8)
        out[j] = enc[(u + 32768) if u < 32768 else (u - 32768)]
    return bytes(out)


def ulaw_to_pcm(u: bytes) -> bytes:
    dec = _ULAW_DECODE
    out = bytearray(len(u) * 2)
    for i, b in enumerate(u):
        v = dec[b]
        out[i * 2] = v & 0xFF
        out[i * 2 + 1] = (v >> 8) & 0xFF
    return bytes(out)


# ------------------------------------------------- Chrome-shaped SDP offer ----

def normalize_sdp_chrome(sdp: str) -> str:
    """Rebuild aiortc's gathered offer in the exact shape Chrome sends.

    WRG's SIP stack rejects: multi-algorithm fingerprints, unroutable c=
    lines, non-Chrome attribute ordering. Keeps aiortc's ICE/DTLS material
    and the full codec set (opus/G722/PCMU/PCMA).
    """
    lines = [l for l in sdp.split("\r\n") if l]
    ufrag = pwd = fingerprint = ssrc = ""
    extmaps, candidates, rtpmaps = [], [], []
    for l in lines:
        if l.startswith("a=ice-ufrag:"):
            ufrag = l.split(":", 1)[1]
        elif l.startswith("a=ice-pwd:"):
            pwd = l.split(":", 1)[1]
        elif l.startswith("a=fingerprint:sha-256"):
            fingerprint = l
        elif l.startswith("a=ssrc:"):
            ssrc = l
        elif l.startswith("a=extmap:"):
            extmaps.append(l)
        elif l.startswith("a=candidate:"):
            candidates.append(l)
        elif l.startswith(("a=rtpmap:", "a=fmtp:", "a=rtcp-fb:")):
            rtpmaps.append(l)
    mline = next(l for l in lines if l.startswith("m=audio"))
    pts = mline.split()[3:]
    # carrier media servers pick ONE candidate to stream to; overlays
    # (tailscale), secondary LANs and v6 confuse that choice. Keep ONLY:
    # the srflx public candidate (what the captured app call used) + the
    # primary LAN host that NAT maps to it.
    srflx = [c for c in candidates if "typ srflx" in c]
    keep = list(srflx)
    if srflx:
        # add only the LAN host the srflx raddr points at
        try:
            parts = srflx[0].split(" ")
            raddr = parts[parts.index("raddr") + 1]
            keep += [c for c in candidates if " typ host" in c
                     and f" {raddr} " in c + " " or c.split(" ")[4] == raddr]
        except (ValueError, IndexError):
            pass
    keep = [c for c in keep if c]
    out = [
        "v=0",
        f"o=- {int(time.time())} 2 IN IP4 127.0.0.1",
        "s=-",
        "t=0 0",
        "a=group:BUNDLE 0",
        "a=msid-semantic: WMS",
        "m=audio 9 UDP/TLS/RTP/SAVPF " + " ".join(pts),
        "c=IN IP4 0.0.0.0",
        "a=rtcp:9 IN IP4 0.0.0.0",
        "a=ice-ufrag:" + ufrag,
        "a=ice-pwd:" + pwd,
        "a=ice-options:trickle",
        fingerprint,
        "a=setup:actpass",
        "a=mid:0",
        *extmaps,
        "a=sendrecv",
        "a=rtcp-mux",
        *rtpmaps,
        ssrc,
        *keep,
        "a=end-of-candidates",
    ]
    return "\r\n".join(out) + "\r\n"

# ------------------------------------------------ wire-SDP shapers (captured) --
# Shapes derived from a CDP capture of the desktop app making a real call
# (call connected, opus media). Two problems solved:
#   1. WRG's SIP stack expects Chrome's payload types (opus=111, red=63,
#      CN=13, telephone-event=110/126) - aiortc numbers opus as 96.
#   2. aiortc must apply Mavenir's answer (opus at 111) against its own
#      internal offer (opus at 96). We renumber on the wire in BOTH
#      directions and never touch aiortc's internal state.

_CHROME_PTS = "111 63 9 0 8 13 110 126"

_STATIC_RTPMAPS = [
    "a=rtpmap:63 red/48000/2",
    "a=rtpmap:13 CN/8000",
    "a=rtpmap:110 telephone-event/48000",
    "a=rtpmap:126 telephone-event/8000",
]


def fetch_display_name(access_token) -> str:
    """The account's display name (originatorName) from linesAndDeviceInfo.
    The real app sends the profile name here - not a tel: URI."""
    import requests as _r
    msisdn = config.username()
    r = _r.post(f"{config.ORCHESTRATOR}/linesAndDeviceInfo",
                headers=config.headers(access_token, {
                    "config": "false", "tmoUserId": "true", "unusedLines": "true",
                    "Content-type": "application/json"}),
                data=json.dumps({"applicationCategory": "webdigits",
                                 "deviceModel": "Group-Production-cDIGITS-WebDIGITS",
                                 "deviceName": "tmo-cli"}), timeout=30)
    if r.status_code == 202:
        time.sleep(1.5)
        for inner in sse_poll(r.json().get("statusUrl", ""), access_token):
            cp = inner.get("consumerProfile") or {}
            first, last = cp.get("firstName", ""), cp.get("lastName", "")
            if (first or last) and (first + last).strip():
                return f"{first} {last}".strip()
            for l in inner.get("lines", []):
                d = re.sub(r"\D", "", str(l.get("msisdn") or ""))
                if len(d) == 11 and d.startswith("1"):
                    d = d[1:]
                if d == msisdn and l.get("lineName"):
                    return l["lineName"]
    return config.display_name()


def chrome_wire_offer(aiortc_sdp: str) -> str:
    """aiortc's gathered offer -> Chrome-exact WIRE SDP.

    - m= line forced to Chrome's PT list (opus 96 -> 111)
    - static rtpmaps appended for red/CN/telephone-event
    - o=/c= use the srflx candidate's address when present (the app does)
    - all candidates kept (host v4/v6/overlay/tcp + srflx) - matches the
      captured winning shape; ICE picks a working pair
    """
    lines = [l for l in aiortc_sdp.split("\r\n") if l]
    ufrag = pwd = fingerprint = ""
    extmaps, cands, keep_maps, ssrc_lines = [], [], [], []
    opus_pt = None
    for l in lines:
        if l.startswith("a=ice-ufrag:"):
            ufrag = l.split(":", 1)[1]
        elif l.startswith("a=ice-pwd:"):
            pwd = l.split(":", 1)[1]
        elif l.startswith("a=fingerprint:sha-256"):
            fingerprint = l
        elif l.startswith("a=extmap:"):
            extmaps.append(l)
        elif l.startswith("a=candidate:"):
            cands.append(l)
        elif l.startswith("a=rtpmap:"):
            pt = l.split(":")[1].split()[0]
            if " opus/" in l:
                opus_pt = pt
            else:
                keep_maps.append(l)  # G722/PCMU/PCMA + their siblings
        elif l.startswith(("a=fmtp:", "a=rtcp-fb:")):
            keep_maps.append(l)
        elif l.startswith("a=ssrc:"):
            ssrc_lines.append(l)

    # public address for o=/c= (srflx raddr when available, like the app)
    o_ip = "127.0.0.1"
    for c in cands:
        if "typ srflx" in c:
            parts = c.split(" ")
            # the srflx candidate's OWN address (parts[4]) is the public IP -
            # raddr is the local one. The app puts the public IP in o=/c=.
            if len(parts) > 4 and ":" not in parts[4]:
                o_ip = parts[4]
            break
    # ALWAYS use the srflx public address if present (the captured app
    # offer puts the public IP in o=/c=). Never fall back to a LAN IP —
    # Mavenir sends media to the c= address and LAN IPs blackhole.
    if o_ip == "127.0.0.1":
        for c in cands:
            if " typ host" in c:
                parts = c.split(" ")
                if len(parts) > 4 and ":" not in parts[4] and not parts[4].startswith("100."):
                    o_ip = parts[4]
                    break

    out = [
        "v=0",
        f"o=- {int(time.time() * 1000)} 2 IN IP4 {o_ip}",
        "s=-",
        "t=0 0",
        "a=group:BUNDLE 0",
        "a=extmap-allow-mixed",
        "a=msid-semantic: WMS",
        f"m=audio 9 UDP/TLS/RTP/SAVPF {_CHROME_PTS}",
        "b=AS:80",
        "c=IN IP4 " + o_ip,
        "a=rtcp:9 IN IP4 " + o_ip,
        "a=ice-ufrag:" + ufrag,
        "a=ice-pwd:" + pwd,
        "a=ice-options:trickle",
        fingerprint,
        "a=setup:actpass",
        "a=mid:0",
        *extmaps,
        "a=sendrecv",
        "a=rtcp-mux",
        "a=rtpmap:111 opus/48000/2",
        "a=rtcp-fb:111 transport-cc",
        "a=fmtp:111 minptime=20;useinbandfec=0;cbr=0;maxaveragebitrate=24576",
        *[m for m in keep_maps if not m.startswith(("a=fmtp:", "a=rtcp-fb:"))],
        *_STATIC_RTPMAPS,
        *ssrc_lines,
        *cands,
        "a=end-of-candidates",
    ]
    return "\r\n".join(out) + "\r\n"


def aiortc_answer(their_sdp: str, our_aiortc_sdp: str) -> str:
    """Mavenir's answer (opus at PT 111, telephone-event 110) -> answer
    renumbered to aiortc's own PTs so setRemoteDescription accepts it.
    Keeps their ICE/DTLS material untouched."""
    # aiortc's opus PT
    our_pt = None
    for l in our_aiortc_sdp.split("\r\n"):
        if l.startswith("a=rtpmap:") and " opus/" in l:
            our_pt = l.split(":")[1].split()[0]
            break
    if not our_pt:
        our_pt = "96"
    out = []
    for l in their_sdp.split("\r\n"):
        if not l:
            continue
        if l.startswith("m=audio"):
            parts = l.split()
            pts = [p for p in parts[3:] if p not in ("110", "126")]  # drop tel-event
            pts = [our_pt if p == "111" else p for p in pts]
            if our_pt not in pts:
                pts.insert(0, our_pt)
            out.append(" ".join(parts[:3] + pts))
        elif l.startswith("a=rtpmap:111 "):
            out.append(l.replace("a=rtpmap:111 ", f"a=rtpmap:{our_pt} ", 1))
        elif l.startswith(("a=rtpmap:110 ", "a=rtpmap:126 ", "a=fmtp:110",
                           "a=fmtp:126", "a=rtcp-fb:110", "a=rtcp-fb:126")):
            continue
        elif l.startswith(("a=fmtp:111", "a=rtcp-fb:111")):
            out.append(l.replace(":111", f":{our_pt}", 1))
        else:
            out.append(l)
    return "\r\n".join(out) + "\r\n"


# ------------------------------------------------ async REST wrappers --------
# The synchronous requests.* calls above each BLOCK the event loop for
# 0.5-3s (TLS + SSE). While blocked, aioice's STUN retry timers miss
# their windows and ICE never completes on live calls (the silent-call
# ICE stall). These wrappers run them in worker threads instead.

async def a_manage_session(access_token, tries=4):
    return await asyncio.to_thread(manage_session, access_token, tries)


async def a_register_line(access_token):
    return await asyncio.to_thread(register_line, access_token)


async def a_sse_poll(status_url, access_token, timeout=30):
    return await asyncio.to_thread(sse_poll, status_url, access_token, timeout)
