"""Record the Interlock explainer video.

Each scene is a page in Chromium. We capture it with the DevTools screencast (sharp JPEG frames with
timestamps), lay the Gemini narration over it, then join the scenes with ffmpeg.

  python video/record.py            # all scenes, then the final mp4
  python video/record.py 05-console # re-record some scenes, then rebuild the mp4
"""
import base64
import json
import re
import subprocess
import sys
import time
import urllib.request
import wave
from pathlib import Path

import imageio_ffmpeg
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
BUILD, OUT = ROOT / "build", ROOT / "out"
BASE = "http://localhost:8790"
FILM = (ROOT / "film.html").as_uri()
VW, VH, DPR = 1536, 864, 1.25          # renders at 1920 x 1080
LEAD, TAIL = 0.6, 0.9                  # picture before and after the voice, seconds
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
SCENES = {s["id"]: s for s in json.loads((ROOT / "narration.json").read_text(encoding="utf-8"))}

HIDE = """
  ::-webkit-scrollbar { display: none; } html { scrollbar-width: none; }
  #vcap { position: fixed; left: 50%; bottom: 34px; transform: translateX(-50%); z-index: 9999; max-width: 1080px; width: max-content;
          background: rgba(16, 24, 40, 0.9); color: #fff; font: 500 21px/1.4 "IBM Plex Sans", system-ui, sans-serif; padding: 11px 20px;
          border-radius: 10px; text-align: center; transition: opacity 200ms; opacity: 0; box-shadow: 0 8px 24px rgba(16,24,40,.18); }
"""


def api(path, body=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"}, method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def voice_len(sid):
    with wave.open(str(BUILD / f"{sid}.wav")) as w:
        return w.getnframes() / w.getframerate()


def caption_lines(text, total):
    """Split the narration into caption lines, timed by length across the voice."""
    parts = []
    for s in re.split(r"(?<=[.?!])\s+", text.strip()):
        if len(s) > 96 and ", " in s:              # long sentence: break at the comma nearest the middle
            cuts = [m.start() for m in re.finditer(", ", s)]
            c = min(cuts, key=lambda i: abs(i - len(s) / 2))
            parts += [s[:c + 1], s[c + 2:]]
        elif len(s) > 96 and ": " in s:
            c = s.index(": ")
            parts += [s[:c + 1], s[c + 2:]]
        else:
            parts.append(s)
    weights = [len(p) + 12 for p in parts]
    t, out = LEAD, []
    for p, w in zip(parts, weights):
        out.append((p, t))
        t += total * w / sum(weights)
    return out


class Shot:
    """One scene: a page, its screencast frames and helpers to act at narration times."""

    def __init__(self, ctx, sid):
        self.sid, self.dur = sid, voice_len(sid)
        self.lines = caption_lines(SCENES[sid]["say"], self.dur)
        self.page = ctx.new_page()
        self.frames, self.dir = [], BUILD / "frames" / sid
        self.dir.mkdir(parents=True, exist_ok=True)
        for f in self.dir.glob("*.jpg"):
            f.unlink()

    def when(self, words):
        """Seconds from the scene start when the caption containing `words` begins."""
        for text, t in self.lines:
            if words.lower() in text.lower():
                return t
        raise KeyError(words)

    def wait_until(self, t):
        left = self.t0 + t - time.time()
        if left > 0:
            self.page.wait_for_timeout(left * 1000)

    def start(self):
        self.page.add_style_tag(content=HIDE)
        self.cdp = self.page.context.new_cdp_session(self.page)
        self.cdp.on("Page.screencastFrame", self._frame)
        self.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 90, "maxWidth": 1920, "maxHeight": 1080})
        self.page.wait_for_timeout(400)
        self.t0 = time.time()
        js_lines = [[t, round(s * 1000)] for t, s in self.lines]
        self.page.evaluate("""(lines) => {
            let el = document.getElementById('vcap');
            if (!el) { el = document.createElement('div'); el.id = 'vcap'; document.body.appendChild(el); }
            lines.forEach(([t, ms]) => setTimeout(() => { el.textContent = t; el.style.opacity = 1; }, ms));
        }""", js_lines)
        # small motion so the screencast keeps a frame at the very start
        self.page.evaluate("window.scrollBy(0, 1); window.scrollBy(0, -1)")

    def _frame(self, p):
        i = len(self.frames)
        path = self.dir / f"{i:05d}.jpg"
        path.write_bytes(base64.b64decode(p["data"]))
        self.frames.append((p["metadata"]["timestamp"], path))
        try:
            self.cdp.send("Page.screencastFrameAck", {"sessionId": p["sessionId"]})
        except Exception:
            pass

    def finish(self):
        self.wait_until(LEAD + self.dur + TAIL)
        self.page.wait_for_timeout(150)
        self.cdp.send("Page.stopScreencast")
        self.page.close()
        total = LEAD + self.dur + TAIL
        frames = sorted(self.frames)
        start = max([f for f in frames if f[0] <= self.t0] or frames[:1])
        keep = [start] + [f for f in frames if self.t0 < f[0] < self.t0 + total]
        lines = []
        for k, (ts, path) in enumerate(keep):
            a = max(ts, self.t0) - self.t0
            b = (keep[k + 1][0] - self.t0) if k + 1 < len(keep) else total
            lines += [f"file '{path.as_posix()}'", f"duration {max(0.001, b - a):.4f}"]
        lines.append(f"file '{keep[-1][1].as_posix()}'")
        lst = BUILD / f"{self.sid}.ffconcat"
        lst.write_text("ffconcat version 1.0\n" + "\n".join(lines) + "\n", encoding="utf-8")
        seg = BUILD / f"{self.sid}.mp4"
        fade = f"fade=t=in:st=0:d=0.25:color=white,fade=t=out:st={total - 0.3:.3f}:d=0.3:color=white"
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                        "-i", str(BUILD / f"{self.sid}.wav"),
                        "-filter_complex", f"[0:v]fps=30,scale=1920:1080:flags=lanczos,{fade},format=yuv420p[v];"
                                           f"[1:a]aresample=48000,adelay={int(LEAD * 1000)}:all=1,apad[a]",
                        "-map", "[v]", "-map", "[a]", "-t", f"{total:.3f}",
                        "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-r", "30", "-pix_fmt", "yuv420p", "-color_range", "tv",
                        "-c:a", "aac", "-b:a", "160k", "-ac", "2", str(seg)], check=True)
        print(f"{self.sid}: {total:.1f}s, {len(keep)} frames", flush=True)


# ---------------------------------------------------------------- scenes

def film(ctx, sid, card, cues):
    s = Shot(ctx, sid)
    s.page.set_viewport_size({"width": VW, "height": VH})
    s.page.goto(FILM)
    s.page.wait_for_timeout(1200)
    s.start()
    gaps = [round(c * 1000) if isinstance(c, (int, float)) else round(s.when(c) * 1000) for c in cues]
    s.page.evaluate("([id, gaps]) => play(id, gaps)", [card, gaps])
    s.finish()


def site(ctx, sid, zoom, anchor, setup=None):
    s = Shot(ctx, sid)
    s.page.goto(BASE + "/")
    s.page.wait_for_timeout(2500)
    s.page.add_style_tag(content=".nav { display: none; }")
    s.page.evaluate(f"document.documentElement.style.zoom = '{zoom}'")
    s.page.evaluate("""(sel) => { const el = document.querySelector(sel);
        window.scrollTo(0, el.getBoundingClientRect().top + window.scrollY - 36); }""", anchor)
    if setup:
        setup(s)
    s.page.wait_for_timeout(600)
    return s


def console(ctx, sid):
    s = Shot(ctx, sid)
    s.page.goto(BASE + "/console")
    s.page.wait_for_selector("#mimic svg", timeout=20000)
    s.page.wait_for_timeout(1500)
    return s


def sc_title(ctx):
    film(ctx, "01-title", "title", [0.1, 0.7, 1.4, 2.2])


def explainer(step):
    """Explainer at a given step, without the Back/Next buttons (the narration drives it)."""
    def setup(s):
        s.page.add_style_tag(content=".ex-controls { display: none; } .explainer { margin-bottom: 0; }")
        s.page.evaluate(f"exStop(); exShow({step})")
    return setup


def sc_problem(ctx):
    s = site(ctx, "02-problem", 1.22, "#problem", explainer(0))
    s.start()
    s.wait_until(s.when("But the dependency graph")); s.page.evaluate("exShow(1)")
    s.wait_until(s.when("If both changes run")); s.page.evaluate("exShow(2)")
    s.finish()


def sc_evidence(ctx):
    film(ctx, "03-evidence", "quotes", [0.1, "An agent deleted", "Security teams ask", "A prompt cannot"])


def sc_solution(ctx):
    s = site(ctx, "04-solution", 1.22, "#problem", explainer(1))
    s.start()
    s.wait_until(s.when("Interlock walks the graph")); s.page.evaluate("exShow(3)")
    s.wait_until(s.when("When agent A finishes")); s.page.evaluate("exShow(4)")
    s.finish()


def sc_console(ctx):
    api("/api/sim", {"action": "enforce", "enforce": True})
    api("/api/sim", {"action": "auto_approve", "auto": True})
    api("/api/sim", {"action": "reset", "agents": 24})
    api("/api/sim", {"action": "start"})
    time.sleep(7)
    s = console(ctx, "05-console")
    s.start()
    s.wait_until(s.when("Green cards"))
    held = s.page.locator("#mimic g.node.held").first
    if held.count():
        held.hover()
    s.wait_until(s.when("dark tags")); s.page.mouse.move(5, 300)
    s.finish()


def sc_why(ctx):
    s = console(ctx, "06-why")
    iid = s.page.evaluate("""() => { const a = (S.state.snapshot.active || []).filter(i => i.status === 'waiting' && i.target.endsWith('@prod'));
                                      return a.length ? a[0].id : null; }""")
    s.start()
    s.page.wait_for_timeout(500)
    if iid:
        s.page.evaluate("(id) => openIntent(id)", iid)
    s.wait_until(s.when("each decision"))
    s.page.evaluate("document.querySelector('#sheet details') && (document.querySelector('#sheet details').open = true)")
    s.page.evaluate("const c = document.querySelector('#sheet .content'); c && c.scrollTo({top: c.scrollHeight, behavior: 'smooth'})")
    s.finish()


def sc_off(ctx):
    api("/api/sim", {"action": "auto_approve", "auto": False})
    s = console(ctx, "07-off")
    s.start()
    s.wait_until(1.4)
    s.page.click("label.switch")
    s.finish()


def sc_bench(ctx):
    def grow(s):
        s.page.add_style_tag(content=".seg { transition: width 1400ms cubic-bezier(.2,.7,.2,1); }")
        s.page.evaluate("document.querySelectorAll('.seg').forEach(e => { e.dataset.w = e.style.width; e.style.width = '0'; })")
    s = site(ctx, "08-bench", 1.25, "#results", grow)
    s.start()
    s.page.wait_for_timeout(500)
    s.page.evaluate("document.querySelectorAll('.seg').forEach(e => { e.style.width = ''; })")
    s.finish()


def sc_policy(ctx):
    api("/api/sim", {"action": "enforce", "enforce": True})
    api("/api/sim", {"action": "auto_approve", "auto": False})
    time.sleep(4)
    s = console(ctx, "09-policy")
    s.page.click("#feedTabs button[data-f='denied']")
    s.start()
    s.wait_until(s.when("A staging bot")); s.page.click("#scn-rogue")
    s.wait_until(s.when("A migration of the payments")); s.page.click("#feedTabs button[data-f='all']"); s.page.click("#scn-migration")
    s.wait_until(s.when("Arjun approves"))
    btn = s.page.locator(".approval", has_text="payments-db").locator("[data-ok]")
    if btn.count():
        btn.first.click()
    s.finish()


def sc_deadlock(ctx):
    s = console(ctx, "10-deadlock")
    s.start()
    s.wait_until(1.0)
    s.page.click("#scn-deadlock")
    s.finish()


def sc_graph(ctx):
    s = site(ctx, "11-graph", 1.15, "#model")
    s.start()
    s.wait_until(s.when("The impact zone"))
    s.page.evaluate("""() => { const el = document.querySelector('#how');
        window.scrollTo({top: el.getBoundingClientRect().top + window.scrollY - 30, behavior: 'smooth'}); }""")
    s.page.wait_for_timeout(900)
    s.page.click("#howSteps button[data-step='1']")
    s.wait_until(s.when("One Cypher write")); s.page.click("#howSteps button[data-step='2']")
    s.wait_until(s.when("The audit trail")); s.page.click("#howSteps button[data-step='3']")
    s.finish()


def sc_mcp(ctx):
    s = site(ctx, "12-mcp", 1.3, "#integrate")
    s.start()
    s.finish()


def sc_end(ctx):
    film(ctx, "13-end", "end", [0.1, 0.8, 1.6])


ORDER = [("01-title", sc_title), ("02-problem", sc_problem), ("03-evidence", sc_evidence), ("04-solution", sc_solution),
         ("05-console", sc_console), ("06-why", sc_why), ("07-off", sc_off), ("08-bench", sc_bench),
         ("09-policy", sc_policy), ("10-deadlock", sc_deadlock), ("11-graph", sc_graph), ("12-mcp", sc_mcp), ("13-end", sc_end)]


def join():
    OUT.mkdir(exist_ok=True)
    lst = BUILD / "all.txt"
    lst.write_text("".join(f"file '{(BUILD / f'{sid}.mp4').as_posix()}'\n" for sid, _ in ORDER), encoding="utf-8")
    out = OUT / "interlock-demo.mp4"
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-c", "copy", "-movflags", "+faststart", str(out)], check=True)
    print("wrote", out)


if __name__ == "__main__":
    only = sys.argv[1:]
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": VW, "height": VH}, device_scale_factor=DPR)
        for sid, fn in ORDER:
            if only and sid not in only:
                continue
            fn(ctx)
        b.close()
    api("/api/sim", {"action": "enforce", "enforce": True})
    join()
