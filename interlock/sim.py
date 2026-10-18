"""A fleet of AI agents working on the company at once. Each agent asks Interlock before it
changes anything, works while it holds the lock, renews its lease, and releases when done.
With enforcement off, the same fleet just goes ahead, and the collisions are counted."""
import collections
import random
import threading
import time

from interlock import company, core
from interlock.db import rows

ROLES = [
    # role, action pool, envs, priority, count weight
    ("deployer", ["deploy", "deploy", "deploy", "restart"], ("prod", "staging"), 2, 10),
    ("migrator", ["migrate", "rotate_secret"], ("prod", "staging"), 2, 3),
    ("flagger", ["flip_flag"], ("prod",), 2, 2),
    ("scaler", ["scale", "scale", "diagnose"], ("prod",), 3, 3),
    ("tester", ["deploy", "restart", "migrate"], ("staging",), 3, 2),
]
TEAM_LEAD = {"Checkout": "priya", "Payments": "priya", "Catalog": "neha", "Growth": "neha", "Fulfilment": "rahul"}


class Fleet:
    def __init__(self):
        self.thread = None
        self.running = False
        self.enforce = True
        self.size = 24
        self.events = collections.deque(maxlen=600)
        self.seq = 0
        self.agents = {}
        self.metrics = self._blank()
        self.lock = threading.RLock()
        self.rng = random.Random(7)
        self.auto_approve = False
        self.crashed = set()
        self.resources = {}

    def _blank(self):
        return {"on": {"completed": 0, "prevented": 0, "waits": 0, "wait_time": 0.0, "denied": 0, "approvals": 0,
                       "deadlocks": 0, "preemptions": 0, "lapsed": 0, "max_parallel": 0},
                "off": {"completed": 0, "collisions": 0, "incidents": 0, "max_parallel": 0}}

    # ------------------------------------------------------------ setup
    def reset(self, size=None, rebuild=True):
        with self.lock:
            self.running = False
        if self.thread:
            self.thread.join(timeout=3)
        if size:
            self.size = size
        if rebuild:
            core.build()
        self.events.clear()
        self.seq = 0
        self.metrics = self._blank()
        self.crashed = set()
        self.resources = {r["id"]: r for r in rows("MATCH (r:Resource) RETURN r.id AS id, r.kind AS kind, r.team AS team, "
                                                    "r.env AS env, r.tier AS tier, r.name AS name")}
        self.agents = {}
        pool = [r for r in ROLES for _ in range(r[4])]
        teams = [t for t in company.TEAMS]
        for i in range(self.size):
            role, actions, envs, prio, _ = pool[i % len(pool)]
            team = teams[i % len(teams)]
            if role == "flagger":
                team = ["Checkout", "Growth", "Payments"][i % 3]
            aid = f"{role}-{i + 1:02d}"
            human = TEAM_LEAD.get(team, "arjun") if role in ("deployer", "flagger") else "arjun"
            core.add_agent(aid, aid, role, prio, envs=envs, authorized_by=human)
            self.agents[aid] = {"id": aid, "role": role, "team": team, "actions": actions, "envs": envs, "prio": prio,
                                "state": "idle", "intent": None, "until": 0.0, "since": 0.0, "next": self.rng.uniform(0, 6),
                                "task": None, "followup": None}
        # standing cast for the scenarios
        core.add_agent("incident-responder", "incident-responder", "sre", 0, authorized_by="arjun")
        core.add_agent("staging-cleaner", "staging-cleaner", "cleaner", 3, envs=("staging",), authorized_by="arjun")
        core.add_agent("reco-tuner", "reco-tuner", "deployer", 3, authorized_by="neha")
        core.add_agent("payments-subagent", "payments-subagent", "deployer", 2, delegated_by="reco-tuner")
        core.add_agent("claude-code", "claude-code (you)", "assistant", 2, authorized_by="priya", kind="mcp")
        core.add_agent("batch-bot-a", "batch-bot-a", "batch", 2, authorized_by="arjun")
        core.add_agent("batch-bot-b", "batch-bot-b", "batch", 3, authorized_by="arjun")
        self.emit("system", None, f"Fleet of {self.size} agents ready. Interlock is {'on' if self.enforce else 'off'}.")

    def emit(self, kind, agent, text, intent=None, verdict=None, extra=None):
        self.seq += 1
        self.events.append({"seq": self.seq, "at": round(core.clock.now(), 1), "kind": kind, "agent": agent,
                            "text": text, "intent": intent, "verdict": verdict, "extra": extra or {}})

    # ------------------------------------------------------------ control
    def start(self):
        with self.lock:
            if self.running:
                return
            if not self.agents:
                self.reset()
            self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def pause(self):
        with self.lock:
            self.running = False

    def set_enforce(self, on):
        with self.lock:
            self.enforce = bool(on)
            # release everything in flight so the two modes don't mix
            for a in self.agents.values():
                if a["intent"]:
                    try:
                        core.release(a["intent"], "aborted")
                    except Exception:
                        pass
                a.update(state="idle", intent=None, next=core.clock.now() + self.rng.uniform(0, 3), followup=None)
            self.emit("system", None, f"Interlock switched {'ON: every change is checked against the graph' if on else 'OFF: agents act without checking'}.")

    # ------------------------------------------------------------ the loop
    def _pick_task(self, a):
        action = self.rng.choice(a["actions"])
        kinds = core.VALID_TARGETS[action]
        env = self.rng.choice(a["envs"])
        reserved = {"email", "sms"}  # kept free for the deadlock lever
        cands = [r for r in self.resources.values() if r["kind"] in kinds and r["env"] == env and r["name"] not in reserved and
                 (r["team"] == a["team"] or a["role"] in ("migrator", "scaler") or self.rng.random() < 0.04)]
        if not cands:
            cands = [r for r in self.resources.values() if r["kind"] in kinds and r["env"] == env and r["name"] not in reserved]
        # busy money paths get more traffic, the way real fleets crowd the important services
        weights = [3 if r["tier"] == 1 else 2 if r["tier"] == 2 else 1 for r in cands]
        target = self.rng.choices(cands, weights)[0]
        follow = None
        if action == "migrate" and self.rng.random() < 0.5:
            owners = [x for x, y, k in company.DEPENDS if y == target["name"] and k in ("writes", "reads")]
            if owners:
                follow = ("deploy", f"{owners[0]}@{target['env']}")
        return action, target["id"], follow

    def _loop(self):
        while True:
            with self.lock:
                if not self.running:
                    return
                try:
                    self._tick()
                except Exception as e:  # keep the fleet alive; show the problem
                    self.emit("error", None, f"{type(e).__name__}: {e}")
            time.sleep(0.25)

    def _statuses(self, ids):
        if not ids:
            return {}
        return {r["id"]: r["status"] for r in rows("MATCH (n:Intent) WHERE n.id IN $ids RETURN n.id AS id, n.status AS status",
                                                   {"ids": ids})}

    def _tick(self):
        now = core.clock.now()
        mode = "on" if self.enforce else "off"
        st = self._statuses([a["intent"] for a in self.agents.values() if a["intent"]])
        for a in self.agents.values():
            if a["id"] in self.crashed:
                continue
            s = st.get(a["intent"]) if a["intent"] else None
            if a["state"] == "working":
                if s in ("preempted", "aborted", "expired"):
                    self.emit("bump", a["id"], f"{a['id']}'s {a['task'][0]} of {a['task'][1].split('@')[0]} was {s}. It will retry.",
                              a["intent"], s)
                    a.update(state="idle", intent=None, next=now + self.rng.uniform(2, 5), followup=None)
                elif now >= a["until"]:
                    core.release(a["intent"]) if self.enforce else core.release(a["intent"])
                    self.metrics[mode]["completed"] += 1
                    if a["followup"]:
                        act, tgt = a["followup"]
                        a["followup"] = None
                        self._request(a, act, tgt, now, follow=None)
                    else:
                        a.update(state="idle", intent=None, next=now + self.rng.uniform(1, 6))
                else:
                    core.heartbeat(a["intent"])
            elif a["state"] == "waiting":
                if s == "granted":
                    waited = now - a["since"]
                    self.metrics["on"]["wait_time"] += waited
                    self.emit("granted", a["id"], f"{a['id']} may now {a['task'][0]} {a['task'][1].split('@')[0]} "
                              f"(waited {waited:.0f}s).", a["intent"], "granted")
                    a.update(state="working", until=now + self.rng.uniform(6, 16))
                elif s in ("aborted", "denied", "expired", "preempted", None):
                    a.update(state="idle", intent=None, next=now + self.rng.uniform(2, 5), followup=None)
            elif a["state"] == "approval":
                if s == "granted":
                    a.update(state="working", until=now + self.rng.uniform(6, 14))
                elif s == "waiting":
                    a.update(state="waiting", since=now)
                elif s == "denied":
                    a.update(state="idle", intent=None, next=now + 8)
                elif self.auto_approve and now - a["since"] > 6:
                    core.approve(a["intent"], "arjun")
            elif a["state"] == "idle" and now >= a["next"]:
                act, tgt, follow = self._pick_task(a)
                self._request(a, act, tgt, now, follow)
        if self.enforce:
            for l in core.sweep():
                self.metrics["on"]["lapsed"] += 1
                self.emit("lapsed", None, "A lease lapsed because its agent stopped renewing it. Waiters can go.", l, "expired")
        working = sum(1 for a in self.agents.values() if a["state"] == "working")
        self.metrics[mode]["max_parallel"] = max(self.metrics[mode]["max_parallel"], working)

    def _request(self, a, action, target, now, follow):
        r = core.request(a["id"], action, target, f"{a['role']} task", enforce=self.enforce)
        a.update(intent=r.get("intent"), task=(action, target), since=now, followup=follow)
        name = target.split("@")[0]
        env = target.split("@")[1]
        label = f"{action} {name}" + ("" if env == "prod" else " (staging)")
        if not self.enforce:
            a.update(state="working", until=now + self.rng.uniform(6, 16))
            cols = r.get("collisions", [])
            if cols:
                c = sorted(cols, key=lambda x: x["tier"])[0]
                tier1 = c["tier"] == 1
                self.metrics["off"]["collisions"] += 1
                if tier1:
                    self.metrics["off"]["incidents"] += 1
                self.emit("collision", a["id"], f"{a['id']} started {label} while another change was in flight. "
                          f"They collide at {c['resource'].split('@')[0]}" + (": customer-facing outage." if tier1 else "."),
                          r.get("intent"), "collision", {"resource": c["resource"], "other": c["other"], "tier": c["tier"],
                                                         "all": [x["resource"] for x in cols]})
            if not r.get("collisions"):
                self.emit("unchecked", a["id"], f"{a['id']} started {label} without checking.", r.get("intent"), "unchecked")
            return
        st = r["status"]
        if st == "granted":
            a.update(state="working", until=now + self.rng.uniform(6, 16))
            if r.get("preempted"):
                self.metrics["on"]["preemptions"] += len(r["preempted"])
            self.emit("granted", a["id"], f"{a['id']} may {label}. {r['reason']}", r["intent"], "granted",
                      {"zone": len(r.get("zone", []))})
        elif st == "waiting":
            a["state"] = "waiting"
            self.metrics["on"]["waits"] += 1
            self.metrics["on"]["prevented"] += 1
            self.emit("waiting", a["id"], f"{a['id']} wants to {label}. {r['reason']}", r["intent"], "waiting",
                      {"blocked_by": r.get("blocked_by", [])})
            if r.get("deadlock"):
                self.metrics["on"]["deadlocks"] += 1
                d = r["deadlock"]
                self.emit("deadlock", d["victim"], f"Deadlock between {', '.join(d['names'])}. Rolled back {d['victim']} so the rest can finish.",
                          d["rolled_back"][0], "aborted", d)
        elif st == "needs_approval":
            a["state"] = "approval"
            self.metrics["on"]["approvals"] += 1
            self.emit("approval", a["id"], f"{a['id']} wants to {label}. {r['reason']}", r["intent"], "needs_approval")
        else:
            a.update(state="idle", intent=None, next=now + self.rng.uniform(4, 8), followup=None)
            self.metrics["on"]["denied"] += 1
            self.emit("denied", a["id"], f"{a['id']} wanted to {label}. Denied: {r['reason']}", r.get("intent"), "denied",
                      {"policy": r.get("policy")})

    # ------------------------------------------------------------ scenarios (the levers)
    def scenario(self, name):
        with self.lock:
            now = core.clock.now()
            if name == "incident":
                r = core.request("incident-responder", "restart", "checkout@prod", "checkout 5xx spike", enforce=self.enforce)
                txt = f"Incident: checkout is returning errors. incident-responder asks to restart checkout. {r.get('reason', '')}"
                self.emit("incident", "incident-responder", txt, r.get("intent"), r.get("status"), {"preempted": r.get("preempted")})
                if r.get("preempted"):
                    self.metrics["on"]["preemptions"] += len(r["preempted"])
                self._schedule_release(r.get("intent"), 10)
                return r
            if name in ("freeze_on", "freeze_off"):
                on = name == "freeze_on"
                rows("MATCH (f:Freeze {id: 'prod-freeze'}) SET f.active = $on", {"on": on}, read_only=False)
                self.emit("freeze", None, "Diwali sale code freeze is ON for prod. Only incident fixes, with approval." if on
                          else "Code freeze lifted.")
                return {"freeze": on}
            if name == "rogue":
                r1 = core.request("staging-cleaner", "delete", "orders-db@prod", "clean up old tables", enforce=self.enforce)
                self.emit("denied" if r1.get("status") == "denied" else "collision", "staging-cleaner",
                          f"staging-cleaner tried to delete orders-db in prod. {r1.get('reason', 'Nothing stopped it.')}",
                          r1.get("intent"), r1.get("status"), {"policy": r1.get("policy")})
                r2 = core.request("payments-subagent", "deploy", "payments@prod", "hotfix", enforce=self.enforce)
                self.emit("denied" if r2.get("status") == "denied" else "unchecked", "payments-subagent",
                          f"payments-subagent (spawned by reco-tuner) tried to deploy payments. {r2.get('reason', 'Nothing stopped it.')}",
                          r2.get("intent"), r2.get("status"), {"policy": r2.get("policy")})
                if r1.get("status") == "denied":
                    self.metrics["on"]["denied"] += 1
                if r2.get("status") == "denied":
                    self.metrics["on"]["denied"] += 1
                return {"delete": r1, "subagent": r2}
            if name == "crash":
                holders = [a for a in self.agents.values() if a["state"] == "working" and a["intent"]]
                if not holders:
                    return {"error": "no agent is holding a lock right now"}
                a = self.rng.choice(holders)
                self.crashed.add(a["id"])
                self.emit("crash", a["id"], f"{a['id']} crashed while holding the lock on {a['task'][1].split('@')[0]}. "
                          f"Its lease will lapse in {core.DEFAULT_TTL:.0f}s instead of blocking everyone forever.", a["intent"], "crashed")
                threading.Timer(core.DEFAULT_TTL / max(core.clock.speed, 0.1) + 2, lambda: self._recover(a["id"])).start()
                return {"crashed": a["id"]}
            if name == "deadlock":
                x, y = {"id": "batch-bot-a"}, {"id": "batch-bot-b"}
                r1 = core.request(x["id"], "deploy", "email@prod", "step 1 of 2: new email templates")
                r2 = core.request(y["id"], "deploy", "sms@prod", "step 1 of 2: new SMS vendor")
                self.emit("system", None, "batch-bot-a holds email and batch-bot-b holds sms. "
                          "Each now asks for the other's resource before letting go of its own.")
                r3 = core.request(x["id"], "restart", "sms@prod", "step 2 of 2")
                self.emit("waiting", x["id"], f"batch-bot-a wants sms. {r3.get('reason', '')}", r3.get("intent"), r3.get("status"))
                r4 = core.request(y["id"], "restart", "email@prod", "step 2 of 2")
                self.emit("waiting", y["id"], f"batch-bot-b wants email. {r4.get('reason', '')}", r4.get("intent"), r4.get("status"))
                d = r4.get("deadlock") or r3.get("deadlock")
                if d:
                    self.metrics["on"]["deadlocks"] += 1
                    self.emit("deadlock", d["victim"], f"Deadlock: {' waits for '.join(d['cycle'])}. Found as a cycle in the "
                              f"waits-on graph. Rolled back {d['victim']}; the other finishes.", d["rolled_back"][0], "aborted", d)
                ids = [r.get("intent") for r in (r1, r2, r3, r4)]

                def finish():
                    with self.lock:
                        for i in ids:
                            if i and self._statuses([i]).get(i) == "granted":
                                core.release(i)
                threading.Timer(8 / max(core.clock.speed, 0.1) + 4, finish).start()
                return {"deadlock": d}
            if name == "migration":
                mig = next((a for a in self.agents if a.startswith("migrator")), next(iter(self.agents)))
                r = core.request(mig, "migrate",
                                 "payments-db@prod", "re-shard payments ledger", enforce=self.enforce)
                self.emit("approval" if r.get("status") == "needs_approval" else r.get("status", "x"), "migrator",
                          f"Big change: re-shard payments-db. {r.get('reason', '')}", r.get("intent"), r.get("status"))
                return r
        return {"error": "unknown scenario"}

    def _recover(self, aid):
        with self.lock:
            self.crashed.discard(aid)
            a = self.agents.get(aid)
            if a:
                a.update(state="idle", intent=None, next=core.clock.now() + 5)
                self.emit("system", aid, f"{aid} restarted.")

    def _schedule_release(self, iid, secs):
        if not iid:
            return

        def done():
            with self.lock:
                st = self._statuses([iid]).get(iid)
                if st == "granted":
                    core.release(iid)
                    self.emit("system", "incident-responder", "Checkout restarted and healthy. incident-responder released its lock.")
                elif st in ("waiting", "needs_approval"):
                    threading.Timer(3, done).start()
        threading.Timer(secs / max(core.clock.speed, 0.1), done).start()

    # ------------------------------------------------------------ reading
    def state(self):
        snap = core.snapshot()
        pending = rows("""MATCH (n:Intent {status: 'needs_approval'})-[:BY]->(a:Agent)
                          OPTIONAL MATCH (d:Decision)-[:ABOUT]->(n)
                          WITH n, a, d ORDER BY d.at DESC
                          WITH n, a, collect(d)[0] AS d
                          RETURN n.id AS intent, n.action AS action, n.target AS target, a.name AS agent,
                                 n.approver AS approver, d.reason AS reason""")
        freeze = rows("MATCH (f:Freeze {id: 'prod-freeze'}) RETURN f.active AS on")
        return {"running": self.running, "enforce": self.enforce, "size": self.size, "speed": core.clock.speed, "seq": self.seq,
                "metrics": self.metrics, "snapshot": snap, "pending": pending,
                "freeze": bool(freeze and freeze[0]["on"]), "crashed": sorted(self.crashed),
                "agents": [{k: a[k] for k in ("id", "role", "team", "state", "intent")} for a in self.agents.values()]}

    def events_after(self, seq):
        return [e for e in list(self.events) if e["seq"] > seq]


fleet = Fleet()
