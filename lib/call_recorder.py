"""
Call recorder: persistent logs + Whisper transcription for Aria calls.

Runs alongside the aria daemons. Watches:
  - /tmp/mic_dump.ulaw  — caller audio (what Aria hears) [daemon dump]
  - gpt_events.log     — her SAYS transcripts + events
and produces, per call:
  - recordings/call_<ts>/caller.ulaw     (full caller audio)
  - recordings/call_<ts>/aria.wav        (her side, decoded from feed)
  - recordings/call_<ts>/transcript.txt   (merged, timestamped)
Caller side is transcribed in 30s windows with Whisper; her side is
already text (SAYS events) — both merge into the transcript.

Usage: python lib/call_recorder.py
"""

import sys
import time
import wave
import array
import os
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config  # noqa: E402

ROOT = config.ROOT
REC = ROOT / "recordings"
BIAS = 0x84


def dec(u: int) -> int:
    u = (~u) & 0xFF
    t = ((u & 0x0F) << 3) + BIAS
    t <<= (u & 0x70) >> 4
    return (BIAS - t) if (u & 0x80) else (t - BIAS)


def ulaw_to_wav(ulaw: bytes, path: Path):
    pcm = array.array("h", (dec(b) for b in ulaw))
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(pcm.tobytes())


def whisper_transcribe(wav_path: Path) -> str:
    from openai import OpenAI
    from dotenv import dotenv_values
    key = dotenv_values(ROOT / ".env")["OAI_API_KEY"]
    cli = OpenAI(api_key=key)
    with open(wav_path, "rb") as f:
        r = cli.audio.transcriptions.create(model="whisper-1", file=f)
    return r.text


def main():
    mic_src = Path("/tmp/mic_dump.ulaw")
    ev_src = ROOT / "gpt_events.log"
    call_dir = None
    pos = 0        # read position in mic dump
    ev_pos = 0     # line position in events log
    pending_wav = b""
    print("[recorder] watching for calls...")

    while True:
        time.sleep(2)
        # --- call start detection: any active ARIA-IS-ON / mic flowing ---
        try:
            log = (ROOT / "bridge_run.log").read_text()
        except FileNotFoundError:
            continue
        on_call = "ARIA IS ON" in log
        if on_call and call_dir is None:
            ts = time.strftime("%Y%m%d_%H%M%S")
            call_dir = REC / f"call_{ts}"
            call_dir.mkdir(parents=True, exist_ok=True)
            pos = mic_src.stat().st_size if mic_src.exists() else 0
            with open(call_dir / "meta.txt", "w") as f:
                f.write(f"call started {time.strftime('%H:%M:%S')}\n")
            print(f"[recorder] call started -> {call_dir.name}")
        if not on_call and call_dir is not None:
            # call ended: flush + transcribe everything
            if mic_src.exists():
                data = mic_src.read_bytes()[pos:]
                if data:
                    (call_dir / "caller.ulaw").write_bytes(data)
                    ulaw_to_wav(data, call_dir / "caller_side.wav")
                    try:
                        txt = whisper_transcribe(call_dir / "caller_side.wav")
                        (call_dir / "caller_transcript.txt").write_text(txt)
                        print(f"[recorder] caller said: {txt[:120]}")
                    except Exception as e:
                        print(f"[recorder] whisper err: {e}")
            # her side: pull SAYS lines from events since start
            try:
                lines = ev_src.read_text().split("\n")
                says = [l for l in lines[ev_pos:] if " SAYS " in l]
                (call_dir / "aria_transcript.txt").write_text(
                    "\n".join(says))
            except FileNotFoundError:
                pass
            print(f"[recorder] call ended -> {call_dir.name} archived")
            call_dir = None
            continue
        if call_dir is None:
            continue

        # --- during call: track events position + live-append caller ---
        try:
            n_lines = len(ev_src.read_text().split("\n"))
            if ev_pos == 0:
                ev_pos = n_lines   # start from now
        except FileNotFoundError:
            pass
        # rolling caller audio to disk (no whisper yet; end-of-call runs it)
        if mic_src.exists():
            sz = mic_src.stat().st_size
            if sz > pos:
                with open(call_dir / "caller.ulaw", "ab") as f:
                    f.write(mic_src.read_bytes()[pos:])
                pos = sz


if __name__ == "__main__":
    main()
