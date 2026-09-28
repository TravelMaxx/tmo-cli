"""
Aria headless: place DIGITS calls with the app's exact technology —
Chromium's native WebRTC stack — driven fully from Python, no app.

Why: our own aiortc leg stalls at ICE 'checking' on live calls, while
the app's Chromium pc connects in ~1s every time. The difference is
the WebRTC implementation, so we use Chromium's — in our own headless
page. No DIGITS app needed, no CDP attach, no hooking.

Flow:
  1. tokens/config from the CLI's saved session (tmo login state)
  2. manageSession + register_line (DaaS REST, same as the CLI)
  3. headless Chromium page builds an RTCPeerConnection via addInitScript
  4. Python sends call/start with the page's offer; SSE poll returns
     Mavenir's answer; we push it into the page's pc
  5. The Aria payload runs ON THE PAGE (same proven payload as the
     app-takeover): her track replaces nothing (the page's mic track IS
     hers), GPT-Live session stays in Python, audio crosses via CDP.

Usage: python lib/aria_headless.py --target 5551234567
"""

import argparse
import asyncio
import base64
import json
import sys
import threading
import time
import uuid
from pathlib import Path
from queue import SimpleQueue

sys.path.insert(0, str(Path(__file__).parent))
import config  # noqa: E402

PERSONA = (config.ROOT / "lib" / "persona.txt").read_text()


def evlog(msg):
    with open(config.ROOT / "gpt_events.log", "a") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")


def load_tokens():
    return config.load_tokens()


# ---------------------------------------------------------------------------
# PAGE SCRIPTS (run in our own headless Chromium — no hooking needed, we
# own the page). RTCPeerConnection is Chrome's native implementation.
# ---------------------------------------------------------------------------
INIT_JS = """
window.__pc = null;
window.__callState = {offer: null, answer: null, ice: 'new', dtls: 'new',
                      inbound: false, her: 0, mic: 0};

window.__makeCall = async () => {
  // native Chrome mic track = Aria's voice source placeholder; the page
  // mic is muted and replaced by GPT audio via the sender.
  const stream = await navigator.mediaDevices.getUserMedia(
    {audio: {echoCancellation: false, noiseSuppression: false,
             autoGainControl: false}});
  const micTrack = stream.getAudioTracks()[0];
  micTrack.enabled = false;   // never send the page mic

  const pc = new RTCPeerConnection({
    iceServers: [{urls: ['stun:stun.l.google.com:19302']}]
  });
  window.__pc = pc;

  // her audio track (WebAudio, fed from Python via __ariaFeed)
  const ctx48 = new AudioContext({sampleRate: 48000});
  const herDest = ctx48.createMediaStreamDestination();
  window.__herDest = herDest;
  window.__ctx48 = ctx48;
  const BIAS = 0x84;
  const ULAW_D = new Float32Array(256);
  for (let i = 0; i < 256; i++) {
    const u = ~i & 0xFF;
    let t = ((u & 0x0F) << 3) + BIAS; t <<= (u & 0x70) >> 4;
    ULAW_D[i] = ((u & 0x80) ? (BIAS - t) : (t - BIAS)) / 32768;
  }
  window.__ULAW_D = ULAW_D;
  window.__nextTime = 0;
  window.__ariaFeed = (b64) => {
    try {
      const bin = atob(b64);
      const n = bin.length; if (!n) return;
      const buf = ctx48.createBuffer(1, n, 8000);
      const ch = buf.getChannelData(0);
      for (let i = 0; i < n; i++) ch[i] = ULAW_D[bin.charCodeAt(i) & 0xFF];
      const src = ctx48.createBufferSource();
      src.buffer = buf; src.connect(herDest);
      const now = ctx48.currentTime;
      if (window.__nextTime < now + 0.05) window.__nextTime = now + 0.05;
      src.start(window.__nextTime);
      window.__nextTime += buf.duration;
      window.__callState.her += n;
    } catch (e) {}
  };

  // mic placeholder so negotiation has an audio m-line; replaceTrack swaps
  // in the her track as soon as GPT audio flows
  pc.addTrack(micTrack);
  const sender = pc.getSenders().find(s => s.track && s.track.kind === 'audio');
  window.__sender = sender;
  // put her (silent) track on the sender NOW — chrome negotiates her stream
  await sender.replaceTrack(herDest.stream.getAudioTracks()[0]);

  pc.addEventListener('iceconnectionstatechange', () => {
    window.__callState.ice = pc.iceConnectionState;
  });
  pc.addEventListener('connectionstatechange', () => {
    window.__callState.dtls = pc.connectionState;
  });
  pc.addEventListener('track', (e) => {
    if (e.track.kind === 'audio') {
      window.__inbound = e.track;
      window.__callState.inbound = true;
    }
  });

  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);
  // wait for ICE gathering
  await new Promise((res) => {
    if (pc.iceGatheringState === 'complete') return res();
    const t = setTimeout(res, 8000);
    pc.addEventListener('icegatheringstatechange', () => {
      if (pc.iceGatheringState === 'complete') { clearTimeout(t); res(); }
    });
  });
  window.__callState.offer = pc.localDescription.sdp;
  return 'offer-ready';
};

window.__applyAnswer = async (sdp) => {
  await window.__pc.setRemoteDescription({type: 'answer', sdp: sdp});
  return 'answer-applied';
};

// caller audio capture: 48k ctx on inbound track, decimate 6:1, ulaw, buffer
window.__micBuf = [];
window.__startMicTap = async () => {
  const inb = window.__inbound;
  if (!inb) return 'no-inbound';
  const ctx = new AudioContext({sampleRate: 48000});
  await ctx.resume();
  const src = ctx.createMediaStreamSource(new MediaStream([inb]));
  const proc = ctx.createScriptProcessor(4096, 1, 1);
  const sink = ctx.createGain(); sink.gain.value = 0;
  src.connect(proc); proc.connect(sink); sink.connect(ctx.destination);
  // ulaw encode table (invert-decoder construction — proven)
  const BIAS = 0x84;
  const ULAW_D = window.__ULAW_D;
  const decPcm = new Int32Array(256);
  for (let b = 0; b < 256; b++) decPcm[b] = Math.round(ULAW_D[b] * 32768);
  const order = Array.from({length: 256}, (_, i) => i)
      .sort((x, y) => decPcm[x] - decPcm[y]);
  const enc = new Uint8Array(65536);
  for (let s = -32768; s < 32768; s++) {
    let lo = 0, hi = 255;
    while (lo < hi) { const mid = (lo + hi) >> 1;
      if (decPcm[order[mid]] < s) lo = mid + 1; else hi = mid; }
    let best = order[lo];
    if (lo > 0 && Math.abs(decPcm[order[lo-1]] - s) <
        Math.abs(decPcm[order[lo]] - s)) best = order[lo-1];
    enc[s + 32768] = best;
  }
  proc.onaudioprocess = (e) => {
    const f = e.inputBuffer.getChannelData(0);
    const n = Math.floor(f.length / 6);
    const out = new Uint8Array(n);
    for (let i = 0; i < n; i++) {
      let acc = 0;
      for (let j = 0; j < 6; j++) acc += f[i*6+j];
      const s = Math.max(-32768, Math.min(32767, Math.round((acc/6)*32768)));
      out[i] = enc[s + 32768];
    }
    window.__callState.mic += out.length;
    let bin = '';
    for (let k = 0; k < out.length; k++) bin += String.fromCharCode(out[k]);
    window.__micBuf.push(btoa(bin));
    if (window.__micBuf.length > 300) window.__micBuf.splice(0, 150);
  };
  return 'mic-tap-live';
};
"""

# ---------------------------------------------------------------------------
# GPT-Live session — identical proven pattern
# ---------------------------------------------------------------------------
def start_gpt(q_in: SimpleQueue, q_out: SimpleQueue, state: dict):
    from openai import AsyncOpenAI

    def thread_main():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def run():
            evlog("HL_GPT_CONNECTING")
            cli = AsyncOpenAI(api_key=state["oai_key"])
            conn = await cli.live.connect().__aenter__()

            async def recv():
                await conn.session.start(
                    event_id=f"ev_{uuid.uuid4().hex[:8]}",
                    session={
                        "model": "gpt-live-1",
                        "instructions": PERSONA,
                        "delegation": {"type": "client"},
                        "audio": {"format": {"type": "audio/pcmu",
                                             "rate": 8000},
                                  "output": {"voice": "marin"}},
                    })
                async for ev in conn:
                    et = getattr(ev, "type", None)
                    if et == "session.started":
                        state["gpt_ready"] = True
                        evlog(f"HL_SESSION_STARTED {ev.session.id}")
                    elif et == "session.output_audio.delta":
                        q_out.put(base64.b64decode(ev.delta))
                    elif et == "session.input_transcript.delta":
                        evlog(f"HEARD {ev.delta[:100]}")
                    elif et == "session.output_transcript.delta":
                        evlog(f"SAYS {ev.delta[:100]}")
                    elif et == "error":
                        state["gpt_error"] = str(ev)[:200]
                        evlog(f"HL_ERROR {str(ev)[:150]}")

            async def pump():
                import base64 as b64mod
                while not state.get("gpt_ready"):
                    await asyncio.sleep(0.05)
                evlog("HL_PUMP_ARMED")
                sent = 0
                t_start = asyncio.get_event_loop().time()
                greeted = False
                while True:
                    budget = int((asyncio.get_event_loop().time() - t_start)
                                  * 8000)
                    room = budget - sent
                    if room <= 0:
                        await asyncio.sleep(0.02)
                        continue
                    chunk = None
                    try:
                        chunk = q_in.get_nowait()
                    except Exception:
                        pass
                    if chunk is None:
                        chunk = b"\xff" * min(800, room)
                    payload = chunk[:room]
                    try:
                        await conn.session.input_audio.append(
                            audio=b64mod.b64encode(payload).decode())
                    except Exception:
                        return
                    sent += len(payload)
                    if not greeted:
                        greeted = True
                        try:
                            await conn.session.instructions.append(
                                event_id=f"go_{uuid.uuid4().hex[:6]}",
                                delegation_id=None,
                                content=("The call just connected — greet the "
                                         "caller NOW, warmly and naturally: "
                                         "'Hey! I'm Aria, the personal "
                                         "assistant. How can I help?' Then "
                                         "pause and listen."))
                            evlog("HL_GREETING_SENT")
                        except Exception as e:
                            evlog(f"HL_GREETING_ERR {e}")

            await asyncio.gather(recv(), pump())

        try:
            loop.run_until_complete(run())
        except Exception as e:
            state["gpt_error"] = str(e)[:200]
            evlog(f"HL_THREAD_DIED {e}")

    threading.Thread(target=thread_main, daemon=True).start()


# ---------------------------------------------------------------------------
async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True, help="10-digit number")
    ap.add_argument("--max", type=int, default=240)
    args = ap.parse_args()

    from dotenv import dotenv_values
    state = {"gpt_ready": False, "gpt_error": None,
             "oai_key": dotenv_values(config.ROOT / ".env")["OAI_API_KEY"]}

    at = config.access_token()
    msisdn = config.username()
    target_digits = config.normalize_msisdn(args.target)
    print(f"[*] line +1{msisdn} -> +1{target_digits} | Aria headless (Chrome WebRTC)")

    # ---- proven DIGITS flow: session + channel + register --------------
    cu, st = await call_stack.a_manage_session(at)
    print(f"1. manageSession -> {st}")
    if not cu:
        raise SystemExit("[!] no channelUrl")
    ch = call_stack.ChromeChannel(log_file=str(config.ROOT / "call_ws.log"))
    if not ch.start(cu):
        raise SystemExit("[!] WS channel failed")
    ok, msg = await call_stack.a_register_line(at)
    print(f"2. register -> {msg}")
    if not ok:
        ch.stop()
        raise SystemExit("[!] register failed")

    # ---- headless Chromium page: NATIVE Chrome WebRTC pc ---------------
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True, args=[
        "--no-sandbox",
        "--autoplay-policy=no-user-gesture-required",
        "--use-fake-ui-for-media-stream"])
    page = await browser.new_page()
    await page.add_init_script(INIT_JS)
    await page.goto("about:blank")
    r = await page.evaluate("window.__makeCall()")
    print(f"3. page pc: {r}")
    offer = None
    for _ in range(40):
        offer = await page.evaluate("window.__callState.offer")
        if offer:
            break
        await asyncio.sleep(0.25)
    if not offer:
        raise SystemExit("[!] no offer from page pc")
    wire_sdp = call_stack.chrome_wire_offer(offer)
    print(f"   offer ready (wire {len(wire_sdp)}B, chrome-native)")

    display = await asyncio.to_thread(call_stack.fetch_display_name, at)
    print(f"   originatorName: {display!r}")
    body = {"vvoipSessionInformation": {
        "originatorAddress": f"sip:1{msisdn}",
        "originatorName": display,
        "receiverAddress": f"sip:+1{target_digits}",
        "receiverName": f"sip:+1{target_digits}",
        "sdp": wire_sdp,
        "clientCorrelator": str(uuid.uuid4()),
        "resourceURL": ""}}
    import requests
    r = None
    for attempt in range(5):
        body["vvoipSessionInformation"]["clientCorrelator"] = str(uuid.uuid4())
        r = await asyncio.to_thread(
            requests.post, f"{config.CALL}/start",
            headers=config.headers(at, {"Content-type": "application/json"}),
            data=json.dumps(body), timeout=30)
        print(f"4. call/start try {attempt+1} -> {r.status_code}")
        if r.status_code in (222, 500):
            await asyncio.sleep(5)
            continue
        break
    if r.status_code not in (200, 201, 202):
        print("   ", r.text[:300])
        raise SystemExit("[!] call/start failed")
    print("   *** AI AGENT DIALING — ANSWER AND TALK ***")

    # ---- wait for the answer SDP on the WS channel (proven path) -------
    answer_sdp = None
    connected = False
    t0 = time.time()
    while time.time() - t0 < 90 and not connected:
        if ch.new_msg.is_set():
            ch.new_msg.clear()
            while not ch.q.empty():
                note = ch.q.get()
                if not isinstance(note, dict):
                    continue
                status = note.get("sessionStatusNotification")
                if not status:
                    continue
                st = status.get("status")
                rc = status.get("responseCode")
                print(f"5. NOTIFICATION status={st!r} rc={rc} "
                      f"sdp={'yes' if status.get('sdp') else 'no'}")
                if st == "Connected" and rc == 200 and status.get("sdp"):
                    answer_sdp = status["sdp"]
                if st == "Terminated":
                    print("   call terminated pre-ring")
        if answer_sdp:
            break
        await asyncio.sleep(0.3)
    if not answer_sdp:
        raise SystemExit("[!] no answer SDP")

    # apply in the page (fix codecs in the answer for chrome like aiortc
    # path did — chrome is stricter about rtpmap ordering; reuse the fixer)
    try:
        fixed = call_stack.aiortc_answer(answer_sdp, offer)
    except Exception:
        fixed = answer_sdp
    r = await page.evaluate(
        f"window.__applyAnswer({json.dumps(fixed)})")
    print(f"6. {r} — chrome-native ICE/DTLS negotiating")
    for _ in range(60):
        d = json.loads(await page.evaluate(
            "JSON.stringify(window.__callState)"))
        if d["ice"] in ("connected", "completed"):
            break
        if d["ice"] in ("failed", "closed"):
            raise SystemExit(f"[!] ICE {d['ice']}")
        await asyncio.sleep(0.5)
    print(f"   ICE {d['ice']} — MEDIA UP (chrome's own stack)")

    # inbound track -> mic tap
    for _ in range(30):
        if await page.evaluate("window.__callState.inbound"):
            break
        await asyncio.sleep(0.5)
    r = await page.evaluate("window.__startMicTap()")
    print(f"7. mic tap: {r}")

    # ---- GPT session + audio loop --------------------------------------
    q_in, q_out = SimpleQueue(), SimpleQueue()
    start_gpt(q_in, q_out, state)
    print("[*] GPT starting; greeting fires on session start")

    t0 = time.time()
    while time.time() - t0 < args.max:
        # her audio -> the call
        if q_out.qsize():
            chunks = []
            while q_out.qsize() and sum(map(len, chunks)) < 32000:
                chunks.append(q_out.get())
            blob = b"".join(chunks)
            await page.evaluate(
                f"window.__ariaFeed('{base64.b64encode(blob).decode()}')")
        # caller audio -> GPT
        rj = await page.evaluate(
            "JSON.stringify((window.__micBuf||[]).splice(0, 60))")
        try:
            for b64 in json.loads(rj):
                q_in.put(base64.b64decode(b64))
        except Exception:
            pass
        # call status notes (Terminated = hang up)
        if ch.new_msg.is_set():
            ch.new_msg.clear()
            while not ch.q.empty():
                note = ch.q.get()
                if isinstance(note, dict):
                    s = note.get("sessionStatusNotification", {})
                    if s.get("status") == "Terminated":
                        print("   call ended")
                        d = {"ice": "closed"}
        if int(time.time() - t0) % 10 == 0:
            d = json.loads(await page.evaluate(
                "JSON.stringify(window.__callState)"))
            print(f"    ice={d['ice']} mic={d['mic']}B her={d['her']}B "
                  f"gpt={'ready' if state['gpt_ready'] else 'connecting'}")
        if state["gpt_error"]:
            print(f"[!] gpt: {state['gpt_error']}")
            state["gpt_error"] = None
        await asyncio.sleep(0.1)

    print("[*] done")
    try:
        await page.evaluate("window.__pc.close()")
    except Exception:
        pass
    await browser.close()
    await pw.stop()
    ch.stop()


if __name__ == "__main__":
    asyncio.run(main())
