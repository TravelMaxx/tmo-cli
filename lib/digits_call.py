#!/usr/bin/env python3
"""DIGITS voice call — the captured, verified flow.

Everything here is shaped by a CDP capture of the desktop app making a
real connected call (opus media):

  1. manageSession -> channelUrl; connect WS channel (ping + nsAck)
  2. register
  3. aiortc offer -> chrome_wire_offer (Chrome PTs 111/63/..., srflx IP)
  4. POST /call/start:
       originatorAddress: sip:<11-digit>        (no plus!)
       originatorName:   <account display name> (fetched live)
       receiverAddress:  sip:+1XXXXXXXXXX
  5. NO REST afterwards — notifications arrive on WS:
       InProgress/180  (no SDP)  -> phone is ringing
       Connected/200   (SDP!)    -> apply answer (renumbered to aiortc PTs)
       Terminated                -> call over
  6. Our-side hangup -> POST /call/end

Usage: tmo call --target 5551234567 [--hold 30]
"""

import argparse
import asyncio
import json
import time
import uuid

import requests
from aiortc import (RTCPeerConnection, RTCSessionDescription,
                    RTCConfiguration, RTCIceServer)
from aiortc.mediastreams import AudioStreamTrack

import config
import call_stack


async def run(target, hold):
    at = config.access_token()
    msisdn = config.username()
    target_digits = config.normalize_msisdn(target)
    print(f"[*] line +1{msisdn} (sip:1{msisdn}) -> sip:+1{target_digits}")

    # 1. session + channel (this channel will own notification routing)
    cu, st = call_stack.manage_session(at)
    print(f"1. manageSession -> {st}")
    if not cu:
        raise SystemExit("[!] no channelUrl")
    ch = call_stack.ChromeChannel(log_file=str(config.ROOT / "call_ws.log"))
    if not ch.start(cu):
        raise SystemExit("[!] WS channel failed")

    # 2. register
    ok, msg = call_stack.register_line(at)
    print(f"2. register -> {msg}")
    if not ok:
        ch.stop()
        return

    # 3. offer (aiortc native, then Chrome-shaped for the wire)
    pc = RTCPeerConnection(configuration=RTCConfiguration(
        iceServers=[RTCIceServer(urls=["stun:stun.l.google.com:19302"])]))
    pc.addTrack(AudioStreamTrack())

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

    # 4. call/start with the captured field shapes
    display = call_stack.fetch_display_name(at)
    print(f"   originatorName: {display!r}")
    body = {"vvoipSessionInformation": {
        "originatorAddress": f"sip:1{msisdn}",  # captured: 11-digit w/ leading 1, no plus
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
        return
    call_res = r.json().get("vvoipSessionInformation", {}).get("resourceURL", "")
    status_hdrs = config.headers(at, {
        "Content-type": "application/json",
        "callResourceUrl": call_res.replace("/conference/", "/sessions/")})
    print(f"\n   *** DIALING +1{target_digits} — ANSWER WHEN IT RINGS ***\n")

    # 5. WS-driven state machine (no REST status POSTs — the app sends none)
    state = {"connected": False, "rtp": 0, "ended": False}
    deadline = time.time() + 120

    def hunt_sdp_notif(content):
        if not isinstance(content, dict):
            return None
        n = content.get("sessionStatusNotification")
        return n

    while time.time() < deadline and not state["ended"]:
        ch.new_msg.wait(timeout=0.5)
        ch.new_msg.clear()
        for raw in ch.drain():
            try:
                content = json.loads(raw)
            except Exception:
                continue
            n = hunt_sdp_notif(content)
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
                fixed = call_stack.aiortc_answer(sdp, pc.localDescription.sdp)
                try:
                    await pc.setRemoteDescription(
                        RTCSessionDescription(sdp=fixed, type="answer"))
                    state["connected"] = True
                    print("   *** CALL CONNECTED (answer applied) ***")
                except Exception as e:
                    print(f"   [!] answer application failed: {e}")
                    state["ended"] = True

        # inbound media watch
        if state["connected"]:
            for tr in pc.getTransceivers():
                if tr.receiver.track and tr.receiver.track.kind == "audio":
                    try:
                        await asyncio.wait_for(tr.receiver.track.recv(), timeout=0.1)
                        state["rtp"] += 1
                        if state["rtp"] == 1:
                            print("   [rtp] FIRST INBOUND AUDIO FRAME — MEDIA LIVE")
                    except asyncio.TimeoutError:
                        pass
                    except Exception:
                        pass
        if state["connected"] and time.time() - t0 > (hold + 120):
            break

    # 6. our-side hangup
    if not state["ended"]:
        try:
            requests.post(f"{config.CALL}/end", headers=status_hdrs, timeout=15,
                          data=json.dumps({"callResourceUrl": call_res}))
            print("   call/end sent (our side)")
        except Exception:
            pass
    print(f"\n6. result: connected={state['connected']} rtp={state['rtp']} "
          f"ice={pc.iceConnectionState}")
    ch.stop()
    try:
        await pc.close()
    except Exception:
        pass
    print("\n[" + ("CALL COMPLETE — MEDIA FLOWED" if state["rtp"]
                  else "CONNECTED (no frames counted)" if state["connected"]
                  else "call did not connect") + "]")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True, help="10-digit number to call")
    ap.add_argument("--hold", type=int, default=20)
    a = ap.parse_args()
    asyncio.run(run(a.target, a.hold))
