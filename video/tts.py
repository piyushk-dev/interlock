"""Narration with Gemini TTS. Reads GEMINI_API_KEY from the environment or a .env file path in TTS_ENV_FILE.
   python video/tts.py <out.wav> "text"  |  python video/tts.py --script video/narration.json"""
import base64, json, os, sys, time, urllib.request, wave
from pathlib import Path

def key():
    k = os.getenv("GEMINI_API_KEY")
    if not k and os.getenv("TTS_ENV_FILE"):
        for line in Path(os.environ["TTS_ENV_FILE"]).read_text(encoding="utf-8").splitlines():
            line = line.strip().removeprefix("export ")
            if line.startswith("GEMINI_API_KEY=") or line.startswith("NEXT_PUBLIC_GEMINI_API_KEY="):
                k = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not k:
        sys.exit("no GEMINI_API_KEY")
    return k

MODEL = os.getenv("TTS_MODEL", "gemini-2.5-flash-preview-tts")
VOICE = os.getenv("TTS_VOICE", "Charon")
STYLE = os.environ["TTS_STYLE"] if "TTS_STYLE" in os.environ else ("Read this like a calm, confident product explainer narrator. Natural pace, warm, clear, not salesy:")

def speak(text, out):
    body = {"contents": [{"parts": [{"text": f"{STYLE}\n{text}"}]}],
            "generationConfig": {"responseModalities": ["AUDIO"],
                                 "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": VOICE}}}}}
    req = urllib.request.Request(f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
                                 data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "x-goog-api-key": key()})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read())
            break
        except urllib.error.HTTPError as e:
            msg = e.read().decode()[:300]
            if e.code in (429, 500, 503) and attempt < 3:
                time.sleep(8 * (attempt + 1)); continue
            sys.exit(f"HTTP {e.code}: {msg}")
    pcm = base64.b64decode(data["candidates"][0]["content"]["parts"][0]["inlineData"]["data"])
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(pcm)
    return len(pcm) / 2 / 24000

if __name__ == "__main__":
    if sys.argv[1] == "--script":
        scenes = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        outdir = Path(sys.argv[3] if len(sys.argv) > 3 else "video/build")
        for s in scenes:
            out = outdir / f"{s['id']}.wav"
            if out.exists() and not os.getenv("TTS_FORCE"):
                continue
            print(s["id"], round(speak(s["say"], out), 2), flush=True)
    else:
        print(round(speak(sys.argv[2], sys.argv[1]), 2))
