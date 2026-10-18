"""Screenshots of the running Interlock panel.  python scripts/shots.py [base_url] [scene ...]"""
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].startswith("http") else "http://localhost:8790"
ONLY = [a for a in sys.argv[1:] if not a.startswith("http")]
OUT = Path(__file__).resolve().parent.parent / "docs" / "screens"
OUT.mkdir(parents=True, exist_ok=True)


def api(page, path, body=None):
    return page.evaluate("""async ([p, b]) => { const r = await fetch(p, b ? {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(b)} : {}); return r.json(); }""", [path, body])


def running(page, enforce=True, secs=9):
    page.goto(BASE + "/console")
    page.wait_for_selector("#mimic svg", timeout=20000)
    api(page, "/api/sim", {"action": "pause"})
    api(page, "/api/sim", {"action": "enforce", "enforce": enforce})
    api(page, "/api/sim", {"action": "reset", "agents": 24})
    api(page, "/api/sim", {"action": "start"})
    page.wait_for_timeout(secs * 1000)


def scene_live(page):
    running(page)


def scene_off(page):
    running(page, enforce=False, secs=7)


def scene_card(page):
    running(page, secs=8)
    iid = page.evaluate("() => { const r = [...document.querySelectorAll('#rows [data-i]')].find(x => x.querySelector('.badge.waiting')) || document.querySelector('#rows [data-i]'); return r && r.dataset.i; }")
    page.evaluate("(id) => openIntent(id)", iid)
    page.wait_for_selector("#sheet:not([hidden]) .timeline", timeout=10000)
    page.wait_for_timeout(500)


def scene_agents(page):
    running(page, secs=7)
    page.click("button[data-view='agents']")
    page.wait_for_timeout(1500)


def scene_policies(page):
    running(page, secs=3)
    page.click("button[data-view='policies']")
    page.wait_for_timeout(900)


def scene_approval(page):
    running(page, secs=4)
    api(page, "/api/scenario", {"name": "migration"})
    page.wait_for_timeout(2200)


def scene_deadlock(page):
    running(page, secs=4)
    api(page, "/api/scenario", {"name": "deadlock"})
    page.wait_for_timeout(1500)


SCENES = {n[6:]: f for n, f in list(globals().items()) if n.startswith("scene_")}
with sync_playwright() as p:
    b = p.chromium.launch()
    for name, fn in SCENES.items():
        if ONLY and name not in ONLY:
            continue
        for label, size, dpr in [("desktop", {"width": 1440, "height": 900}, 1), ("mobile", {"width": 390, "height": 844}, 2)]:
            if label == "mobile" and name != "live":
                continue
            ctx = b.new_context(viewport=size, device_scale_factor=dpr)
            page = ctx.new_page()
            errs = []
            page.on("pageerror", lambda e: errs.append(str(e)))
            page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
            try:
                fn(page)
                page.screenshot(path=str(OUT / f"{name}-{label}.png"), full_page=(label == "mobile"))
                print("ok", name, label, errs[:3])
            except Exception as e:
                print("FAIL", name, label, type(e).__name__, str(e)[:200], errs[:3])
            ctx.close()
    b.close()
