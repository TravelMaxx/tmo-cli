"""
Aria-in-the-app: ride the DIGITS desktop app's WebRTC via CDP.

The app owns the carrier media leg (ICE/DTLS/SRTP — always works, zero
fraud exposure). We attach to its renderer, hook the live call's
RTCPeerConnection, and swap the outbound mic track for Aria's voice
(WebAudio), while the caller's audio is shipped back to Python for the
GPT-Live session (which must run here — the SDK needs Authorization
headers the renderer's WebSocket API can't send).

Flow (place the call from the app, answer, then run this):
  python lib/aria_app_bridge.py

Prereq (one-time per app launch): the RTCPeerConnection hook must be
installed in the renderer — this script injects it if missing. pcs
created BEFORE the hook are invisible, so: launch bridge first, then
place the call.

Usage: tmo aria-app
"""

import argparse
import asyncio
import base64
import json
import sys
import threading
import time
import urllib.request
import uuid
from queue import SimpleQueue

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
import config  # noqa: E402

CDP_PORT = 9222


def load_api_key():
    from dotenv import dotenv_values
    return dotenv_values(config.ROOT / ".env")["OAI_API_KEY"]


PERSONA = (config.ROOT / "lib" / "persona.txt").read_text()


# --------------------------------------------------------------------------
# The renderer-side payload. Installed once; survives navigation-free
# lifetime of the app. __ariaFeed(b64) pushes her audio, the mic
# ScriptProcessor calls binding ariaMic with caller audio.
# --------------------------------------------------------------------------
HOOK_JS = r"""
(() => {
  if (window.__pcHooked) return 'hooked-already';
  window.__pcs = []; window.__pcEvents = []; window.__inbound = []; window.__outbound = [];
  const OrigPC = window.RTCPeerConnection;
  if (!OrigPC) return 'no-rtc';
  const Patched = function(...args) {
    const pc = new OrigPC(...args);
    const idx = window.__pcs.push(pc) - 1;
    pc.addEventListener('track', (e) => { window.__inbound.push(e.track);
      window.__pcEvents.push('track:'+e.track.kind+':pc'+idx); });
    const origAdd = pc.addTrack.bind(pc);
    pc.addTrack = (track, ...rest) => {
      const sender = origAdd(track, ...rest);
      window.__outbound.push({pc: idx, track, sender});
      window.__pcEvents.push('addTrack:'+track.kind+':pc'+idx);
      return sender;
    };
    window.__pcEvents.push('created:'+idx);
    return pc;
  };
  Patched.prototype = OrigPC.prototype;
  window.RTCPeerConnection = Patched;
  window.__pcHooked = true;
  return 'hooked-ok';
})()
"""

ARIA_JS = r"""
(() => {
  if (window.__aria) return 'aria-already';
  const BIAS = 0x84;
  const ULAW_D = new Float32Array(256);
  for (let i = 0; i < 256; i++) {
    const u = ~i & 0xFF;
    let t = ((u & 0x0F) << 3) + BIAS; t <<= (u & 0x70) >> 4;
    ULAW_D[i] = ((u & 0x80) ? (BIAS - t) : (t - BIAS)) / 32768;
  }
  // Encoder built by INVERTING the verified decoder above — for each of
  // the 256 ulaw bytes, compute the PCM it decodes to; encode any PCM
  // value as the byte whose decode is nearest. Self-consistent by
  // construction (the hand-rolled algebra version was sign-broken).
  const decPcm = new Int32Array(256);
  for (let b = 0; b < 256; b++) decPcm[b] = Math.round(ULAW_D[b] * 32768);
  const order = Array.from({length: 256}, (_, i) => i)
      .sort((a, b) => decPcm[a] - decPcm[b]);
  const enc = new Uint8Array(65536);
  for (let s = -32768; s < 32768; s++) {
    let lo = 0, hi = 255;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (decPcm[order[mid]] < s) lo = mid + 1; else hi = mid;
    }
    let best = order[lo];
    if (lo > 0 &&
        Math.abs(decPcm[order[lo - 1]] - s) < Math.abs(decPcm[order[lo]] - s))
      best = order[lo - 1];
    enc[s + 32768] = best;
  }
  window.__aria = {state: 'installed', mic: 0, her: 0};
  // mic shim: ring buffer Python pulls via Runtime.evaluate (CDP bindings
  // proved unreliable across bridge restarts)
  window.__micBuf = window.__micBuf || [];
  // EXCLUSIVE by construction: whatever stale capture handlers from older
  // payload installs are still running, they ALL call window.__ariaMic.
  // Only audio from the CURRENT payload's capture context is kept —
  // every push is tagged with the owner id; others are dropped. This
  // kills the 2x-rate double-feed even when old closures survive.
  window.__ariaMicOwner = null;
  window.__ariaMic = (b64) => {
    if (window.__ariaMicOwner !== null &&
        window.__ariaMicTag !== window.__ariaMicOwner) return;   // stale tap: drop
    window.__micBuf.push(b64);
    if (window.__micBuf.length > 300) window.__micBuf.splice(0, 150);
  };
  // KILL all capture contexts from any PREVIOUS payload install — else
  // old + new both push into __micBuf and GPT input runs at 2x real-time
  window.__allCapCtxs = window.__allCapCtxs || [];
  for (const c of window.__allCapCtxs) { try { c.close(); } catch (e) {} }
  window.__allCapCtxs = [];
  const a = window.__aria;

  // 48k context: her voice -> MediaStreamTrack
  a.ctx48 = new AudioContext({sampleRate: 48000});
  a.herDest = a.ctx48.createMediaStreamDestination();
  a.nextTime = 0;

  a.feed = (b64) => {           // called from Python with her PCMU chunks
    try {
      const bin = atob(b64);
      const n = bin.length; if (!n) return;
      const buf = a.ctx48.createBuffer(1, n, 8000);
      const ch = buf.getChannelData(0);
      for (let i = 0; i < n; i++) ch[i] = ULAW_D[bin.charCodeAt(i) & 0xFF];
      const src = a.ctx48.createBufferSource();
      src.buffer = buf; src.connect(a.herDest);
      const now = a.ctx48.currentTime;
      // BOTH-WAYS clamp: nextTime must sit in [now+0.05, now+2]. It ran
      // 355s ahead once (accumulated across payload generations while
      // each new AudioContext restarted its clock at 0) — she spoke into
      // a 6-minute-delay queue and the caller heard nothing.
      if (a.nextTime < now + 0.05 || a.nextTime > now + 2) a.nextTime = now + 0.05;
      src.start(a.nextTime); a.nextTime += buf.duration;
      a.her += n;
    } catch (e) { a.state = 'feed-err:' + String(e).slice(0,80); }
  };
  window.__ariaFeed = a.feed;
  // GV relay never returns caller voice on the inbound track (verified
  // via receiver audioLevel stats) — the caller talks into the Mac mic.
  a.useMacMic = true;

  // mic capture: NATIVE-RATE ScriptProcessor (8k ctx resamples the track
  // to silence in Chromium — verified: 48k analyser saw the voice, the
  // 8k SP saw zeros), then decimate 6:1 to 8k for ulaw/PCMU.
  a.attach = (track) => {
    try {
      // FORCE 48k: default ctx is 44.1k on this machine -> /6 gives
      // 7350 Hz mislabeled as 8k = ~9% pitch/speed garble (GPT heard
      // speech-shaped noise: 'I think I missed that'). 48k/6 = exactly 8k.
      const ctx = new AudioContext({sampleRate: 48000});
      a.capCtx = ctx;
      // tag: this capture's pushes own the mic buffer
      window.__ariaMicOwner = 'aria-' + Date.now();
      const ratio = 6;
      const src = ctx.createMediaStreamSource(new MediaStream([track]));
      const proc = ctx.createScriptProcessor(4096, 1, 1);
      const sink = ctx.createGain(); sink.gain.value = 0;
      src.connect(proc); proc.connect(sink); sink.connect(ctx.destination);
      proc.onaudioprocess = (e) => {
        const f = e.inputBuffer.getChannelData(0);
        const n = Math.floor(f.length / ratio);
        const out = new Uint8Array(n);
        for (let i = 0; i < n; i++) {
          // average native samples per 8k sample (anti-alias-ish);
          // ratio handles 44.1k AND 48k contexts
          let acc = 0;
          for (let j = 0; j < ratio; j++) acc += f[i * ratio + j];
          const s = Math.max(-32768, Math.min(32767,
            Math.round((acc / 6) * 32768)));
          out[i] = enc[s + 32768];
        }
        a.mic += out.length;
        let bin = '';
        for (let k = 0; k < out.length; k++) bin += String.fromCharCode(out[k]);
        if (window.__ariaMic) window.__ariaMic(btoa(bin));   // -> Python
      };
      a.state = 'listening';
      return 'attached';
    } catch (e) { a.state = 'attach-err:' + String(e).slice(0,80); return null; }
  };
  // attach guard keyed on TRACK LIVENESS, not a boolean: GV's bridge
  // replaces the inbound track mid-call (screening-leg track dies when
  // the real leg connects); re-tap the new track automatically.
  const _attach = a.attach;
  a.attach = (track) => {
    if (a.tappedTrack && a.tappedTrack === track &&
        a.tappedTrack.readyState === 'live') return 'already';
    try { if (a.capCtx) a.capCtx.close(); } catch (e) {}
    a.tappedTrack = track;
    // claim the mic buffer for THIS payload; drops every older handler
    window.__ariaMicTag = window.__ariaMicOwner;
    return _attach(track);
  };
  // GV relay never returns caller voice on the inbound track (verified
  // via receiver audioLevel stats: 0.5 during relay announcements,
  // 0.001-0.03 while the caller talks). Aria hears the caller through
  // the Mac's own mic instead.
  a.micFromMac = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia(
        {audio: {echoCancellation: true, noiseSuppression: true,
                 autoGainControl: true, channelCount: 1}});
      const track = stream.getAudioTracks()[0];
      const r = a.attach(track);
      return 'mac-mic: ' + r + ' (' + track.label + ')';
    } catch (e) {
      a.state = 'macmic-err:' + String(e).slice(0,80);
      return 'macmic-err:' + String(e).slice(0,80);
    }
  };
  return 'installed';
})()
"""

GRAB_JS = r"""
(async () => {
  const a = window.__aria; if (!a) return 'aria-missing';
  // latest pc with a live audio sender
  let target = null;
  for (const o of (window.__outbound || [])) {
    if (o.track && o.track.kind === 'audio' && o.sender) target = o;
  }
  if (!target) return 'no-live-sender';
  const pcState = (window.__pcs[target.pc] || {}).iceConnectionState;
  if (pcState !== 'connected' && pcState !== 'completed') return 'ice:' + pcState;
  // her track into the sender (mutes the app mic) — only when it
  // actually differs; hammering replaceTrack 10x/s (re-grab loop)
  // churns Chromium's send path into static
  const her = a.herDest.stream.getAudioTracks()[0];
  if (target.sender.track !== her) {
    await target.sender.replaceTrack(her);
  }
  if (a.useMacMic) {
    // caller audio comes from the Mac mic (GV never returns voice on
  // the inbound track — verified via receiver audioLevel stats). One
  // exclusive tap; never also taps the inbound track.
    if (!a.macMicReady) {
      a.macMicReady = true;
      const r = await a.micFromMac();
      window.__pcEvents.push('MACMIC:' + r);
    }
  } else {
    const inb = (window.__inbound || []).find(t => t.kind === 'audio' && t.readyState === 'live');
    if (!inb) return 'no-inbound';
    a.attach(inb);
  }
  return 'live';
})()
"""

STATE_JS = ("JSON.stringify({state: window.__aria && window.__aria.state, "
            "mic: window.__aria && window.__aria.mic, "
            "her: window.__aria && window.__aria.her})")


# --------------------------------------------------------------------------
# GPT-Live session: same isolated thread/loop pattern proven in
# digits_gpt_call.py (the bridge loop must never touch it).
# --------------------------------------------------------------------------
def start_gpt(q_in: SimpleQueue, q_out: SimpleQueue, state: dict):
    from openai import AsyncOpenAI

    def thread_main():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        evlog("THREAD_START")

        async def run():
            evlog("RUN_ENTER")
            cli = AsyncOpenAI(api_key=load_api_key())
            conn = await cli.live.connect().__aenter__()
            evlog("WS_CONNECTED")
            async def recv():
                evlog("GPT_CONNECTING")
                await conn.session.start(
                    event_id=f"ev_{uuid.uuid4().hex[:8]}",
                    session={
                        "model": "gpt-live-1",
                        "instructions": PERSONA,
                        "delegation": {"type": "client"},
                        "audio": {"format": {"type": "audio/pcmu", "rate": 8000},
                                  "output": {"voice": "marin"}},
                    })
                async for ev in conn:
                    et = getattr(ev, "type", None)
                    if et == "session.started":
                        state["gpt_ready"] = True
                        evlog(f"SESSION_STARTED {ev.session.id}")
                    elif et == "session.output_audio.delta":
                        q_out.put(base64.b64decode(ev.delta))
                    elif et == "session.output_transcript.delta":
                        evlog(f"SAYS {ev.delta[:100]}")
                    elif et == "session.input_transcript.delta":
                        evlog(f"HEARD {ev.delta[:100]}")
                    elif et == "error":
                        state["gpt_error"] = str(ev)[:200]
                        evlog(f"ERROR {str(ev)[:150]}")

            async def pump():
                # caller audio -> GPT at REAL-TIME pace (8000 B/s). The
                # previous 1600B/80ms burst = 2.5x real-time; VAD never
                # saw an end-of-turn, so she never spoke. Mic chunks drain
                # continuously; silence fills only the gaps.
                import base64 as b64mod
                # CRITICAL: never append before session.start — OpenAI
                # kills the whole connection ('The first Live event must
                # be session.start'). Wait for recv() to finish starting.
                t_wait = 0.0
                while not state.get("gpt_ready"):
                    await asyncio.sleep(0.05)
                    t_wait += 0.05
                    if t_wait > 20:
                        evlog("PUMP_GAVE_UP_WAITING")
                        return
                evlog("PUMP_ARMED")
                sent = 0
                t_start = asyncio.get_event_loop().time()
                greeted_seq = 0
                while True:
                    budget = int((asyncio.get_event_loop().time() - t_start) * 8000)
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
                    # one-shot greeting once the session is up (proven
                    # pattern from digits_gpt_call — she speaks only
                    # after an explicit kickoff instruction)
                    # per-call greeting: main loop bumps greet_seq on every
                    # new grabbed call (daemon mode re-greets each call)
                    if state.get("greet_seq", 0) > greeted_seq:
                        greeted_seq = state["greet_seq"]
                        try:
                            await conn.session.instructions.append(
                                event_id=f"go_{uuid.uuid4().hex[:6]}",
                                delegation_id=None,
                                content=("The call just connected — greet the "
                                         "caller NOW, warmly and naturally: "
                                         "'Hey! I'm Aria, the personal "
                                         "assistant. How can I help?' Then "
                                         "pause and listen."))
                            evlog(f"GREETING_SENT seq={greeted_seq}")
                        except Exception as e:
                            evlog(f"GREETING_ERR {e}")

            await asyncio.gather(recv(), pump())

        try:
            loop.run_until_complete(run())
        except Exception as e:
            state["gpt_error"] = str(e)[:200]
            evlog(f"THREAD_DIED {e}")

    threading.Thread(target=thread_main, daemon=True).start()


def evlog(msg):
    with open(config.ROOT / "gpt_events.log", "a") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=0,
                    help="seconds to run; 0 = forever (daemon)")
    args = ap.parse_args()

    import websockets

    targets = json.load(urllib.request.urlopen(f"http://localhost:{CDP_PORT}/json"))
    page = [t for t in targets if t["type"] == "page"][0]
    ws_url = page["webSocketDebuggerUrl"]
    print(f"[*] CDP attached: {page['title'][:40]}")

    q_in, q_out = SimpleQueue(), SimpleQueue()
    state = {"gpt_ready": False, "gpt_error": None, "dump_mic": True}

    async with websockets.connect(ws_url, max_size=10 * 1024 * 1024) as ws:
        next_id = [0]

        async def rpc(method, params=None):
            next_id[0] += 1
            cid = next_id[0]
            payload = {"id": cid, "method": method}
            if params:
                payload["params"] = params
            await ws.send(json.dumps(payload))
            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                if m.get("method") == "Runtime.bindingCalled":
                    p = m.get("params", {})
                    if p.get("name") == "ariaMic":
                        try:
                            q_in.put(base64.b64decode(p.get("payload", "")))
                        except Exception:
                            pass
                    continue
                if m.get("id") == cid:
                    return m

        async def eval_js(expr):
            r = await rpc("Runtime.evaluate",
                          {"expression": expr, "awaitPromise": True,
                           "returnByValue": True})
            res = r.get("result", {}).get("result", {})
            return res.get("value", json.dumps(r.get("result", {}))[:200])

        await rpc("Runtime.enable")
        # expose the mic binding BEFORE anything fires it
        await rpc("Runtime.addBinding", {"name": "ariaMic"})
        print("[*] binding ariaMic installed")

        hook = await eval_js(HOOK_JS)
        print(f"[*] pc hook: {hook}")
        aria = await eval_js(ARIA_JS)
        print(f"[*] aria payload: {aria}")
        if "installed" not in str(aria) and "already" not in str(aria):
            raise SystemExit(f"[!] payload failed: {aria}")

        print("[*] waiting for a live call (place it from the app now)...")
        print("[*] starting GPT session in background thread...")
        start_gpt(q_in, q_out, state)
        # (Bindings proved unreliable across bridge restarts — a dead
        # session can own the name and the audio goes nowhere.)
        def pull_mic():
            pass

        t0 = time.time()
        grabbed = False
        while args.max <= 0 or time.time() - t0 < args.max:
            # 1. grab AND RE-GRAB every iteration: GV's bridge swaps the
            #    inbound track mid-call (screening leg dies at pickup);
            #    the payload's track-liveness guard re-taps the fresh
            #    track, and replaceTrack on the same track is a no-op
            g = await eval_js(GRAB_JS)
            if not grabbed and g == "live":
                grabbed = True
                print("\n*** ARIA IS ON THE CALL — her track on the sender, "
                      "mic streaming to GPT ***\n")
                state["greet_seq"] = state.get("greet_seq", 0) + 1
            elif grabbed and g == "no-inbound":
                # all inbound tracks dead = call over; re-arm for next call
                grabbed = False
                state["voice_seen"] = False
                state["mic_peak_max"] = 0
                evlog("CALL_ENDED re-armed")
                print("[*] call ended — re-armed; every new call gets Aria")
            elif g == "aria-missing":
                # page navigated (e.g. call window re-route) and wiped our
                # injected state — re-arm on the SAME page, immediately
                print("[*] payload wiped by navigation — re-injecting")
                await eval_js(HOOK_JS)
                a = await eval_js(ARIA_JS)
                evlog(f"REINJECTED {a}")
                state["reinjects"] = state.get("reinjects", 0) + 1
            elif g not in ("live", "no-live-sender", "no-inbound") \
                    and not g.startswith("ice:"):
                print(f"[!] grab: {g}")
            # 2. ship her audio into the call
            if grabbed and q_out.qsize():
                chunks = []
                while q_out.qsize() and sum(map(len, chunks)) < 32000:
                    chunks.append(q_out.get())
                blob = b"".join(chunks)
                await eval_js(f"window.__ariaFeed("
                              f"'{base64.b64encode(blob).decode()}')")
            # 3. heartbeat rpc EVERY iteration — the dispatch loop inside
            # rpc() is the only reader of the CDP socket; without a
            # steady rpc, mic binding events queue up for up to 15s and
            # GPT receives burst-garbled audio (she never responds).
            if not (grabbed and q_out.qsize()):
                await eval_js("1")   # drains bindingCalled events
                last_feed = time.time()
            # 3b. pull mic audio from the page ring buffer (bindings are
            # unreliable across bridge restarts — the payload pushes b64
            # chunks into window.__micBuf; we drain here every iteration)
            r = await eval_js("JSON.stringify((window.__micBuf||[]).splice(0, 60))")
            try:
                if state.get("dump_mic"):
                    with open("/tmp/mic_dump.ulaw", "ab") as f:
                        for b64 in json.loads(r):
                            f.write(base64.b64decode(b64))
                mic_peak = 0
                for b64 in json.loads(r):
                    raw = base64.b64decode(b64)
                    # ulaw peak meter: 0x7f/0xff = silence, ~0x00/0x80 = loud
                    for b in raw:
                        v = ~b & 0xFF
                        mag = ((v & 0x0F) << 3) + 0x84
                        mag <<= (v & 0x70) >> 4
                        if mag > mic_peak:
                            mic_peak = mag
                    q_in.put(raw)
                if mic_peak:
                    state["mic_peak_max"] = max(state.get("mic_peak_max", 0),
                                                mic_peak)
                    if mic_peak > 2000 and not state.get("voice_seen"):
                        state["voice_seen"] = True
                        evlog(f"VOICE_SEEN_IN_MIC peak={mic_peak}")
            except Exception:
                pass

            if int(time.time() - t0) % 15 == 0:
                import threading as _th
                st = await eval_js(STATE_JS)
                print(f"    mic_peak_max={state.get('mic_peak_max', 0)} "
                      f"threads={_th.active_count()} gpt_ready={state.get('gpt_ready')} "
                      f"gpt_err={state.get('gpt_error')}")
            if state["gpt_error"] and not state["gpt_ready"]:
                evlog(f"GPT_DIED {state['gpt_error']}")
                print(f"[!] gpt session died: {state['gpt_error']} — respawning")
                state["gpt_error"] = None
                state["gpt_ready"] = False
                start_gpt(q_in, q_out, state)
            await asyncio.sleep(0.1)

        print("[*] daemon window elapsed" if args.max > 0 else "")

# mic binding events arrive as Runtime.bindingCalled notifications on the
# same socket; we consume them inside rpc()'s dispatch loop. To keep this
# module simple we drain them opportunistically via a background task.
async def _noop():
    pass


if __name__ == "__main__":
    # mic events: a dedicated reader interleaves with rpc via a shared queue
    asyncio.run(main())
