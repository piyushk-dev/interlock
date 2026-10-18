import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from interlock import core  # noqa: E402
from interlock.sim import fleet  # noqa: E402

WEB = Path(__file__).resolve().parent.parent / "web"
app = FastAPI(title="Interlock", version="1.0")


@app.on_event("startup")
def boot():
    core.clock.set_speed(float(os.getenv("INTERLOCK_SPEED", "3")))
    fleet.reset(size=int(os.getenv("INTERLOCK_AGENTS", "24")))


# ---------------------------------------------------------------- simulation

class SimIn(BaseModel):
    action: str                      # start | pause | reset | enforce | speed | auto_approve
    agents: int | None = None
    enforce: bool | None = None
    speed: float | None = None
    auto: bool | None = None


@app.post("/api/sim")
def sim(body: SimIn):
    if body.action == "start":
        fleet.start()
    elif body.action == "pause":
        fleet.pause()
    elif body.action == "reset":
        was = fleet.running
        fleet.reset(size=max(4, min(60, body.agents or fleet.size)))
        if was:
            fleet.start()
    elif body.action == "enforce":
        fleet.set_enforce(bool(body.enforce))
    elif body.action == "speed":
        core.clock.set_speed(max(0.5, min(10.0, body.speed or 3)))
    elif body.action == "auto_approve":
        fleet.auto_approve = bool(body.auto)
    else:
        raise HTTPException(400, "unknown action")
    return fleet.state()


class ScenarioIn(BaseModel):
    name: str


@app.post("/api/scenario")
def scenario(body: ScenarioIn):
    return fleet.scenario(body.name)


@app.get("/api/state")
def state():
    return fleet.state()


@app.get("/api/events")
def events(after: int = 0):
    return fleet.events_after(after)


@app.get("/api/agents")
def agents():
    live = {a["id"]: a for a in fleet.state()["agents"]}
    out = []
    for a in core.agents():
        f = live.get(a["id"])
        a["state"] = (f or {}).get("state") or ("working" if any(x["status"] == "granted" for x in a["active"]) else
                                                 "waiting" if a["active"] else "idle")
        if a["id"] in fleet.crashed:
            a["state"] = "crashed"
        a["team"] = (f or {}).get("team")
        out.append(a)
    return out


@app.get("/api/policies")
def policies():
    return core.policies()


@app.get("/api/topology")
def topology(env: str = "prod"):
    return core.topology(env)


@app.get("/api/why")
def why(intent: str):
    return core.why(intent)


class ApproveIn(BaseModel):
    intent: str
    human: str = "arjun"
    yes: bool = True


@app.post("/api/approve")
def approve(body: ApproveIn):
    with fleet.lock:
        r = core.approve(body.intent, body.human, body.yes)
        fleet.emit("approved" if body.yes else "rejected", body.human,
                   f"{body.human} {'approved' if body.yes else 'rejected'} the change. Outcome: {r.get('status')}.",
                   body.intent, r.get("status"))
        return r


# ---------------------------------------------------------------- for real agents (the MCP server calls these)

class RequestIn(BaseModel):
    agent: str = "claude-code"
    action: str
    target: str
    reason: str = ""
    ttl: float = core.DEFAULT_TTL


@app.post("/api/request")
def request(body: RequestIn):
    with fleet.lock:
        target = body.target if "@" in body.target else f"{body.target}@prod"
        r = core.request(body.agent, body.action, target, body.reason, ttl=body.ttl, enforce=fleet.enforce)
        fleet.emit(r.get("status", "x") if r.get("status") != "granted" else "granted", body.agent,
                   f"{body.agent} asked to {body.action} {target.split('@')[0]}. {r.get('reason', '')}",
                   r.get("intent"), r.get("status"))
        return r


class LeaseIn(BaseModel):
    intent: str
    ttl: float = core.DEFAULT_TTL


@app.post("/api/heartbeat")
def heartbeat(body: LeaseIn):
    core.heartbeat(body.intent, body.ttl)
    return {"ok": True}


@app.post("/api/release")
def release(body: LeaseIn):
    with fleet.lock:
        woken = core.release(body.intent)
        fleet.emit("system", None, f"Lock {body.intent} released. {len(woken)} waiting change(s) can go.", body.intent, "done")
        return {"woken": woken}


@app.get("/api/resources")
def resources(q: str = ""):
    from interlock.db import rows
    return rows("""MATCH (r:Resource) WHERE toLower(r.id) CONTAINS toLower($q)
                   RETURN r.id AS id, r.kind AS kind, r.tier AS tier, r.team AS team ORDER BY r.env, r.tier, r.name LIMIT 40""",
                {"q": q})


@app.get("/api/stats")
def stats():
    return core.stats()


@app.get("/api/cypher")
def cypher():
    return [{"name": n[2:].lower(), "cypher": getattr(core, n).strip()} for n in sorted(dir(core)) if n.startswith("Q_")]


@app.get("/health")
def health():
    return {"ok": True, "running": fleet.running}


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/console")
def console():
    return FileResponse(WEB / "console.html")


@app.post("/api/ensure_running")
def ensure_running():
    """The product page embeds the live map; make sure there is something to watch."""
    if not fleet.running:
        fleet.start()
    return {"running": True}


app.mount("/", StaticFiles(directory=WEB), name="web")
