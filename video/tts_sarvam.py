"""Narration with Sarvam Bulbul v3: one Indian-English speaker for every clip, so the voice never changes.
   SARVAM_API_KEY=... python video/tts_sarvam.py video/narration.json video/build"""
import base64
import io
import json
import os
import sys
import time
import urllib.request
import wave
from pathlib import Path

MODEL = os.getenv("SARVAM_MODEL", "bulbul:v3")
SPEAKER = os.getenv("SARVAM_SPEAKER", "shubh")
PACE = float(os.getenv("SARVAM_PACE", "1.0"))
TEMP = float(os.getenv("SARVAM_TEMPERATURE", "0.5"))


def speak(text, out):
    body = {"text": text, "target_language_code": "en-IN", "model": MODEL, "speaker": SPEAKER,
            "pace": PACE, "temperature": TEMP, "speech_sample_rate": 24000}
    req = urllib.request.Request("https://api.sarvam.ai/text-to-speech", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "api-subscription-key": os.environ["SARVAM_API_KEY"]})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read())
            break
        except urllib.error.HTTPError as e:
            msg = e.read().decode()[:300]
            if e.code in (429, 500, 503) and attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            sys.exit(f"HTTP {e.code}: {msg}")
    raw = base64.b64decode(data["audios"][0])
    Path(out).write_bytes(raw)
    with wave.open(io.BytesIO(raw)) as w:
        return w.getnframes() / w.getframerate()


if __name__ == "__main__":
    scenes = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    outdir = Path(sys.argv[2])
    outdir.mkdir(parents=True, exist_ok=True)
    only = sys.argv[3:]
    for s in scenes:
        if only and s["id"] not in only:
            continue
        print(s["id"], round(speak(s["say"], outdir / f"{s['id']}.wav"), 2), flush=True)
