#!/usr/bin/env python3
"""AI voice agent bridge v2 — built ON the proven plain-call flow.

Architecture change from v1: v1 created its own parallel call stack
(offer-flip path, second peer connection mid-call) whose ICE never
completed. This version is lib/digits_call.py's exact flow — one
RTCPeerConnection created once, Mavenir's answer applied once via
call_stack.aiortc_answer, ICE left alone — with exactly three grafts:

  1. the local audio track is a GptLiveTrack (fed by GPT's output)
  2. a GPT-Live session with the DOCS-mandated silence pump (input audio
     must run from session start or she never speaks — live-conversations
     guide, "Greet before the caller speaks")
  3. after the call connects, two audio pumps: caller RTP -> GPT input,
     GPT output -> caller RTP

Usage: tmo ai-call --target 5551234567 [--max 240]
"""

import argparse

import numpy as np
import asyncio
import base64
import json
import time
import uuid
from pathlib import Path

import av
import requests
from aiortc import (RTCPeerConnection, RTCSessionDescription,
                    RTCConfiguration, RTCIceServer)
from openai import AsyncOpenAI

import config
import call_stack

PERSONA_FILE = Path(__file__).resolve().parent / "persona.txt"


def load_api_key():
    key = config.load_env().get("OAI_API_KEY", "")
    if not key:
        raise SystemExit("[!] OAI_API_KEY missing from .env")
    return key


class GptLiveTrack(av.AudioFrame if False else object):
    """Placeholder replaced below — kept for import clarity."""
    pass


def make_gpt_track():
    """Build the aiortc source track fed by GPT output (mu-law bytes)."""
    from aiortc.mediastreams import AudioStreamTrack

    class _Track(AudioStreamTrack):
        kind = "audio"

        def __init__(self):
            super().__init__()
            self.queue = asyncio.Queue()
            self.pts = 0
            self._in_pts = 0
            # 8k -> 48k so frames EXACTLY match the negotiated opus codec
            # (opus is 48kHz-only; 8k frames + mismatched timebase starve
            # the encoder — the silent-call bug)
            self._up = av.AudioResampler(format="s16", layout="mono", rate=48000)

        async def recv(self):
            try:
                ulaw = await asyncio.wait_for(self.queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                ulaw = b"\xff" * 160  # PCMU silence
            burst = bytearray(ulaw)
            while len(burst) < 1600:  # drain queue — send bursts, keep pacing
                try:
                    burst.extend(self.queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            pcm = call_stack.ulaw_to_pcm(bytes(burst))
            in_samples = len(pcm) // 2
            in_frame = av.AudioFrame.from_ndarray(
                np.frombuffer(pcm, dtype="<i2").reshape(1, -1),
                format="s16", layout="mono")
            in_frame.sample_rate = 8000
            in_frame.pts = self._in_pts
            self._in_pts += in_samples
            # exact 6x multiple: 20ms in -> 20ms out, no buffered delay
            out = self._up.resample(in_frame)
            frame = out[0] if isinstance(out, list) else out
            frame.pts = self.pts            # pts in the 48k timebase
            self.pts += frame.samples
            return frame

    return _Track()


async def run(target, max_seconds):
    at = config.access_token()
    msisdn = config.username()
    target_digits = config.normalize_msisdn(target)
    print(f"[*] line +1{msisdn} -> +1{target_digits} | AI voice agent")

    persona = PERSONA_FILE.read_text().strip() if PERSONA_FILE.exists() else (
        "You are a friendly voice assistant on a phone call. Be concise.")
    try:
        owner = call_stack.fetch_display_name(at).split()[0]
    except Exception:
        owner = "the account holder"
    persona = persona.replace("{OWNER}", owner)

    # ---------------- DIGITS session + channel + register (proven order) ----
    cu, st = call_stack.manage_session(at)
    print(f"1. manageSession -> {st}")
    if not cu:
        raise SystemExit("[!] no channelUrl")
    ch = call_stack.ChromeChannel(log_file=str(config.ROOT / "call_ws.log"))
    if not ch.start(cu):
        raise SystemExit("[!] WS channel failed")
    ok, msg = call_stack.register_line(at)
    print(f"2. register -> {msg}")
    if not ok:
        ch.stop()
        return

    # ---------------- GPT-Live session (started early, silence pump) -------
    client = AsyncOpenAI(api_key=load_api_key())
    session_cfg = {
        "model": "gpt-live-1",
        "instructions": persona,
        "delegation": {"type": "client"},
        "audio": {"format": {"type": "audio/pcmu", "rate": 8000},
                  "output": {"voice": "marin"}},
    }
    gpt_in = asyncio.Queue()    # real caller audio (PCMU)
    gpt_out = asyncio.Queue()  # GPT output audio (PCMU)
    track = make_gpt_track()
    stop_event = asyncio.Event()
    counters = {"in_rtp": 0, "silence": 0, "gpt_out_deltas": 0, "gpt_out_bytes": 0}

    print("[gpt] connecting...")
    connection = await client.live.connect().__aenter__()

    EVLOG = config.ROOT / "gpt_events.log"

    def evlog(msg):
        with open(EVLOG, "a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")

    async def gpt_recv():
        try:
            await connection.session.start(
                session=session_cfg, event_id=f"ev_{uuid.uuid4().hex[:8]}")
            async for event in connection:
                et = getattr(event, "type", None)
                if et:
                    evlog(et)
                if et == "session.started":
                    print(f"[gpt] session ready ({event.session.id})")
                    # PROVEN SEQUENCE (standalone 310KB test): instruction
                    # fires ~1s after session start, while silence streams —
                    # NOT at call-connect 10-15s later
                    async def _greet_later():
                        # user pref: wait >=20s before greeting; keep the
                        # session engaged with keep-waiting commentary so the
                        # model doesn't idle out (docs: commentary.append is
                        # context the model can use; delegation_id=None)
                        try:
                            for tick in range(4):  # 5s apart = 20s
                                await asyncio.sleep(5.0)
                                if stop_event.is_set():
                                    return
                                await connection.session.commentary.append(
                                    event_id=f"keep_{tick}_{uuid.uuid4().hex[:4]}",
                                    delegation_id=None,
                                    content=("The line is quiet right now — the "
                                             "caller hasn't spoken yet. Keep "
                                             "waiting patiently and listening. "
                                             "Do not hang up or end anything."))
                                evlog(f"KEEP_WAITING_{tick}")
                            if stop_event.is_set():
                                return
                            await connection.session.instructions.append(
                                event_id=f"gr_{uuid.uuid4().hex[:6]}",
                                delegation_id=None,
                                content=("Now greet the caller in English. Say: "
                                         "'Hey! This is Aria, the personal "
                                         "assistant calling on behalf of the "
                                         "account holder. Just confirming the "
                                         "assistant line works. Talk soon!' "
                                         "Then pause and listen."))
                            print("[gpt] opening instruction sent (after 20s + keepalives)")
                            evlog("INSTRUCTION_SENT")
                        except Exception as e:
                            evlog(f"INSTRUCTION_ERR {e}")
                            print(f"[gpt] instruction failed: {e}")
                    asyncio.create_task(_greet_later())
                elif et == "session.output_audio.delta":
                    d = base64.b64decode(event.delta)
                    counters["gpt_out_deltas"] += 1
                    counters["gpt_out_bytes"] += len(d)
                    if counters["gpt_out_deltas"] == 1:
                        print("[gpt] FIRST OUTPUT DELTA — she is speaking")
                        evlog("FIRST_AUDIO_DELTA")
                    await gpt_out.put(d)
                elif et == "session.output_transcript.delta":
                    print(f"[gpt] says: {event.delta}", end="", flush=True)
                elif et == "error":
                    try:
                        print(f"\n[gpt] ERROR: {event.model_dump_json()[:400]}")
                    except Exception:
                        print(f"\n[gpt] ERROR: {event}")
                elif et == "session.closed":
                    print(f"\n[gpt] closed (usage={getattr(event, 'usage', '')})")
                    stop_event.set()
        except Exception as e:
            if not stop_event.is_set():
                print(f"[gpt] recv loop ended: {e}")

    async def gpt_silence_and_audio():
        """DOCS: input audio must run from session start (even silence).
        Real caller audio (from gpt_in) is forwarded when present."""
        while not stop_event.is_set():
            try:
                real = None
                try:
                    real = gpt_in.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                if real is not None:
                    await connection.session.input_audio.append(
                        audio=base64.b64encode(real).decode("ascii"))
                else:
                    await connection.session.input_audio.append(
                        audio=base64.b64encode(b"\xff" * 160 * 20).decode("ascii"))
                    counters["silence"] += 1
                await asyncio.sleep(0.16)
            except asyncio.CancelledError:
                raise
            except Exception:
                return

    # ---------------- the call: EXACTLY the proven plain-call flow ---------
    pc = RTCPeerConnection(configuration=RTCConfiguration(
        iceServers=[RTCIceServer(urls=["stun:stun.l.google.com:19302"])]))
    pc.addTrack(track)  # <- only graft: GPT-fed track instead of silence

    def on_ice():
        print(f"   [webrtc] ICE {pc.iceConnectionState}")
    pc.on("iceconnectionstatechange")(on_ice)

    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    t0 = time.time()
    while pc.iceGatheringState != "complete" and time.time() - t0 < 8:
        await asyncio.sleep(0.3)
    wire_sdp = call_stack.chrome_wire_offer(pc.localDescription.sdp)
    print(f"3. offer ready (wire {len(wire_sdp)}B, ICE {pc.iceGatheringState})")

    display = call_stack.fetch_display_name(at)
    print(f"   originatorName: {display!r}")
    body = {"vvoipSessionInformation": {
        "originatorAddress": f"sip:1{msisdn}",
        "originatorName": display,
        "receiverAddress": f"sip:+1{target_digits}",
        "receiverName": f"sip:+1{target_digits}",
        "sdp": wire_sdp,
        "clientCorrelator": str(uuid.uuid4()),
        "resourceURL": ""}}
    r = None
    for attempt in range(5):
        body["vvoipSessionInformation"]["clientCorrelator"] = str(uuid.uuid4())
        r = requests.post(f"{config.CALL}/start",
                          headers=config.headers(at, {"Content-type": "application/json"}),
                          data=json.dumps(body), timeout=30)
        print(f"4. call/start try {attempt+1} -> {r.status_code}")
        if r.status_code in (222, 500):
            await asyncio.sleep(5)
            continue
        break
    if r.status_code not in (200, 201, 202):
        print("   ", r.text[:300])
        ch.stop()
        await pc.close()
        try:
            await connection.close()
        except Exception:
            pass
        return
    call_res = r.json().get("vvoipSessionInformation", {}).get("resourceURL", "")
    status_hdrs = config.headers(at, {
        "Content-type": "application/json",
        "callResourceUrl": call_res.replace("/conference/", "/sessions/")})
    print(f"\n   *** AI AGENT DIALING +1{target_digits} — ANSWER AND TALK ***\n")

    state = {"connected": False, "ended": False}
    deadline = time.time() + 120

    async def rtp_to_gpt():
        """caller RTP -> gpt_in (re-resolve track each loop — exists only
        after the answer is applied)."""
        resampler = av.AudioResampler(format="s16", layout="mono", rate=8000)
        while not stop_event.is_set() and not state["ended"]:
            remote = None
            for tr in pc.getTransceivers():
                if tr.receiver.track and tr.receiver.track.kind == "audio":
                    remote = tr.receiver.track
            if remote is None:
                await asyncio.sleep(0.2)
                continue
            try:
                frame = await asyncio.wait_for(remote.recv(), timeout=0.1)
                f = resampler.resample(frame)[0]
                counters["in_rtp"] += 1
                if counters["in_rtp"] == 1:
                    print("[rtp] FIRST INBOUND FRAME — we hear the caller")
                await gpt_in.put(call_stack.pcm_to_ulaw(bytes(f.planes[0])))
            except asyncio.TimeoutError:
                pass
            except Exception:
                pass

    async def gpt_to_rtp():
        """GPT output -> the aiortc track -> caller."""
        while not stop_event.is_set() and not state["ended"]:
            try:
                chunk = await asyncio.wait_for(gpt_out.get(), timeout=0.1)
                await track.queue.put(chunk)
            except asyncio.TimeoutError:
                pass

    async def watchdog():
        # voicemail mode: end the call 30s after connect so the message
        # lands; otherwise the absolute max
        while not stop_event.is_set() and not state["ended"]:
            if state.get("vm_at") and time.time() - state["vm_at"] > 30:
                print("   [vm] 30s message window done — hanging up")
                stop_event.set()
                state["ended"] = True
                return
            await asyncio.sleep(1)
        await asyncio.sleep(max(0, max_seconds - (time.time() - t0)))
        if not stop_event.is_set():
            print(f"\n[bridge] {max_seconds}s limit")
            stop_event.set()
            state["ended"] = True

    tasks = [asyncio.create_task(gpt_recv()),
             asyncio.create_task(gpt_silence_and_audio()),
             asyncio.create_task(rtp_to_gpt()),
             asyncio.create_task(gpt_to_rtp()),
             asyncio.create_task(watchdog())]

    # ---- notification loop: the proven digits_call state machine ----
    try:
        while time.time() < deadline and not state["ended"] and not stop_event.is_set():
            ch.new_msg.wait(timeout=0.5)
            ch.new_msg.clear()
            for raw in ch.drain():
                try:
                    content = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(content, dict):
                    continue
                n = content.get("sessionStatusNotification")
                if not n:
                    continue
                st_, rc = n.get("status", ""), str(n.get("responseCode", ""))
                sdp = n.get("sdp")
                print(f"5. NOTIFICATION status={st_!r} rc={rc} sdp={'yes' if sdp else 'no'}")

                if st_ in ("Terminated", "SessionCancelled"):
                    print(f"   call ended ({rc})")
                    state["ended"] = True
                elif st_ == "Declined" and rc == "486":
                    print("   DECLINED (busy)")
                    state["ended"] = True
                elif st_ == "InProgress" and not sdp:
                    print("   *** RINGING ***")
                elif st_ == "Connected" and rc == "200" and sdp:
                    if state["connected"]:
                        print("   [note] duplicate Connected — ignored")
                        continue
                    fixed = call_stack.aiortc_answer(sdp, pc.localDescription.sdp)
                    try:
                        await pc.setRemoteDescription(
                            RTCSessionDescription(sdp=fixed, type="answer"))
                        state["connected"] = True
                        print("   *** CALL CONNECTED (answer applied) — AI ON THE LINE ***")
                        # opening instruction already sent at session start
                        state["vm_at"] = time.time()
                    except Exception as e:
                        print(f"   [!] answer application failed: {e}")
                        state["ended"] = True
    finally:
        for t_ in tasks:
            t_.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if not state["ended"]:
            try:
                requests.post(f"{config.CALL}/end", headers=status_hdrs, timeout=15,
                              data=json.dumps({"callResourceUrl": call_res}))
                print("   call/end sent")
            except Exception:
                pass
        ch.stop()
        try:
            await pc.close()
        except Exception:
            pass
        try:
            await connection.session.close()
        except Exception:
            pass
        try:
            await connection.close()
        except Exception:
            pass

    print(f"\n6. result: connected={state['connected']} "
          f"in_rtp={counters['in_rtp']} gpt_out={counters['gpt_out_bytes']}B "
          f"ice={pc.iceConnectionState}")
    print("\n[" + ("AI CALL COMPLETE — AUDIO EXCHANGED" if
          (state['connected'] and (counters['in_rtp'] or counters['gpt_out_bytes']))
          else "connected, no audio" if state['connected'] else "call did not connect") + "]")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True)
    ap.add_argument("--max", type=int, default=240)
    a = ap.parse_args()
    asyncio.run(run(a.target, a.max))
