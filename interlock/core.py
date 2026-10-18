"""Interlock: graph-aware locks, policy gates and an audit trail for AI agents that change
production. All state lives in one FalkorDB graph. Every decision is one Cypher query, and
FalkorDB runs each write query atomically, so a conflict check and the grant it leads to can
never interleave with another agent's request.

Nodes: Resource (Service | Datastore | Topic | Flag), Env, Team, Human, Agent, Intent, Decision,
       Freeze, Collision
Edges: (Resource)-[:DEPENDS_ON {kind}]->(Resource)   the dependent points at its dependency
       (Resource)-[:IN]->(Env), (Resource)-[:OWNED_BY]->(Team)
       (Human)-[:CAN_CHANGE]->(Team), (Human)-[:AUTHORIZED]->(Agent), (Agent)-[:DELEGATED_TO]->(Agent)
       (Agent)-[:SCOPED_TO]->(Env)
       (Intent)-[:BY]->(Agent), (Intent)-[:IMPACTS {role, hops}]->(Resource)
       (Intent)-[:WAITS_FOR]->(Intent), (Agent)-[:WAITS_ON]->(Agent)
       (Decision)-[:ABOUT]->(Intent), (Decision)-[:AUTHORIZED_BY]->(Human)
       (Freeze)-[:COVERS]->(Env | Resource)
"""
import itertools
import threading
import time

from interlock import company
from interlock.db import graph, rows, write

# action: (mode, impact depth, destructive)
ACTIONS = {
    "deploy": ("exclusive", 2, False),
    "restart": ("exclusive", 1, False),
    "migrate": ("exclusive", 2, False),
    "rotate_secret": ("exclusive", 1, False),
    "flip_flag": ("exclusive", 2, False),
    "scale": ("shared", 0, False),
    "diagnose": ("shared", 0, False),
    "delete": ("exclusive", 3, True),
}
VALID_TARGETS = {
    "deploy": {"service"}, "restart": {"service"}, "scale": {"service"},
    "migrate": {"datastore"}, "rotate_secret": {"datastore"}, "flip_flag": {"flag"},
    "delete": {"service", "datastore", "topic", "flag"}, "diagnose": {"service", "datastore", "topic", "flag"},
}
APPROVAL_ZONE_SIZE = 14      # a change that ripples into more resources than this needs a human
DEFAULT_TTL = 40.0           # seconds of clock time before an unrenewed lease lapses

_lock = threading.RLock()    # serialises multi-query workflows inside this process
_ids = itertools.count(1)


class Clock:
    """Simulation time that can run faster than the wall clock."""

    def __init__(self):
        self.base_wall = time.time()
        self.base_sim = 0.0
        self.speed = 1.0

    def now(self):
        return self.base_sim + (time.time() - self.base_wall) * self.speed

    def set_speed(self, speed):
        self.base_sim, self.base_wall, self.speed = self.now(), time.time(), speed


clock = Clock()


def new_id(prefix):
    return f"{prefix}-{int(time.time() * 1000) % 10_000_000:07d}-{next(_ids)}"


# ---------------------------------------------------------------- building the company graph

def build(reset=True):
    g = graph()
    if reset:
        try:
            g.delete()
        except Exception:
            pass
    for label, prop in [("Resource", "id"), ("Agent", "id"), ("Intent", "id"), ("Human", "id"), ("Team", "name"),
                        ("Env", "name"), ("Decision", "id"), ("Freeze", "id")]:
        try:
            write(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
        except Exception:
            pass
    write("UNWIND $envs AS e CREATE (:Env {name: e})", {"envs": company.ENVS})
    write("UNWIND $teams AS t CREATE (:Team {name: t})", {"teams": company.TEAMS})
    res = list(company.resources())
    for kind, label in [("service", "Service"), ("datastore", "Datastore"), ("topic", "Topic"), ("flag", "Flag")]:
        write(f"""UNWIND $rows AS r
                  MATCH (e:Env {{name: r.env}}), (t:Team {{name: r.team}})
                  CREATE (n:Resource:{label} {{id: r.id, name: r.name, kind: r.kind, tier: r.tier, env: r.env, team: r.team}})
                  CREATE (n)-[:IN]->(e), (n)-[:OWNED_BY]->(t)""", {"rows": [r for r in res if r["kind"] == kind]})
    write("""UNWIND $rows AS d MATCH (a:Resource {id: d.a}), (b:Resource {id: d.b})
             CREATE (a)-[:DEPENDS_ON {kind: d.kind}]->(b)""", {"rows": list(company.dependencies())})
    for h in company.HUMANS:
        write("""CREATE (h:Human {id: $id, name: $name}) WITH h
                 UNWIND $teams AS t MATCH (tm:Team {name: t}) CREATE (h)-[:CAN_CHANGE]->(tm)""", h)
    write("CREATE (:Freeze {id: 'prod-freeze', reason: 'Diwali sale code freeze', active: false})")
    write("MATCH (f:Freeze {id: 'prod-freeze'}), (e:Env {name: 'prod'}) CREATE (f)-[:COVERS]->(e)")


def add_agent(agent_id, name, role, priority=2, envs=("prod", "staging"), authorized_by=None, delegated_by=None,
              kind="sim"):
    """Register an agent. It must trace back to a human, directly or through a delegating agent."""
    write("""MERGE (a:Agent {id: $id}) SET a.name = $name, a.role = $role, a.priority = $priority, a.kind = $kind,
             a.status = 'idle'
             WITH a OPTIONAL MATCH (a)-[r:SCOPED_TO|WAITS_ON]->() DELETE r""",
          {"id": agent_id, "name": name, "role": role, "priority": priority, "kind": kind})
    write("""MATCH (a:Agent {id: $id}) UNWIND $envs AS e MATCH (en:Env {name: e}) MERGE (a)-[:SCOPED_TO]->(en)""",
          {"id": agent_id, "envs": list(envs)})
    if authorized_by:
        write("MATCH (h:Human {id: $h}), (a:Agent {id: $id}) MERGE (h)-[:AUTHORIZED]->(a)", {"h": authorized_by, "id": agent_id})
    if delegated_by:
        write("MATCH (p:Agent {id: $p}), (a:Agent {id: $id}) MERGE (p)-[:DELEGATED_TO]->(a)", {"p": delegated_by, "id": agent_id})


# ---------------------------------------------------------------- the queries

Q_ZONE = """
MATCH (t:Resource {{id: $target}})
OPTIONAL MATCH p = (d:Resource)-[:DEPENDS_ON*1..{depth}]->(t)
WITH t, d, min(length(p)) AS hops
RETURN t.id AS target, collect(CASE WHEN d IS NULL THEN NULL ELSE {{id: d.id, hops: hops, tier: d.tier}} END) AS zone
"""

Q_AUTHORITY = """
MATCH (a:Agent {id: $agent}), (r:Resource {id: $target})-[:OWNED_BY]->(team:Team)
OPTIONAL MATCH (h:Human)-[:AUTHORIZED]->(top:Agent)-[d:DELEGATED_TO*0..3]->(a)
WHERE (h)-[:CAN_CHANGE]->(team)
RETURN team.name AS team, h.id AS human, h.name AS human_name,
       CASE WHEN h IS NULL THEN [] ELSE [h.name] + [x IN nodes(d) | x.name] END AS chain
ORDER BY length(d) LIMIT 1
"""

Q_ENV_SCOPE = """
MATCH (a:Agent {id: $agent}), (r:Resource {id: $target})-[:IN]->(e:Env)
RETURN e.name AS env, size([(a)-[:SCOPED_TO]->(e) | 1]) > 0 AS allowed,
       [(a)-[:SCOPED_TO]->(x:Env) | x.name] AS scope
"""

Q_FREEZE = """
MATCH (r:Resource {id: $target})
OPTIONAL MATCH (f:Freeze {active: true})-[:COVERS]->(x)
WHERE x = r OR (r)-[:IN]->(x)
RETURN f.id AS freeze, f.reason AS reason
"""

# The heart of Interlock: one atomic query that checks the new intent's impact against every
# granted intent and either grants it or records exactly who it waits for and where they meet.
Q_TRY_GRANT = """
MATCH (n:Intent {id: $id})
OPTIONAL MATCH (n)-[a:IMPACTS]->(r:Resource)<-[b:IMPACTS]-(g:Intent {status: 'granted'})
WHERE g <> n AND (
      (a.role = 'target' AND n.mode = 'exclusive')
   OR (b.role = 'target' AND g.mode = 'exclusive')
   OR (r.tier = 1 AND n.mode = 'exclusive' AND g.mode = 'exclusive'))
WITH n, collect(DISTINCT g) AS blockers, collect(DISTINCT {resource: r.id, intent: g.id}) AS meets
SET n.status = CASE WHEN size(blockers) = 0 THEN 'granted' ELSE 'waiting' END,
    n.granted_at = CASE WHEN size(blockers) = 0 THEN $now ELSE n.granted_at END,
    n.expires_at = CASE WHEN size(blockers) = 0 THEN $now + n.ttl ELSE n.expires_at END
FOREACH (g IN blockers | MERGE (n)-[:WAITS_FOR]->(g))
RETURN n.status AS status, [g IN blockers | g.id] AS blockers, meets
"""

Q_AGENT_WAITS = """
MATCH (n:Intent {id: $id})-[:BY]->(me:Agent), (n)-[:WAITS_FOR]->(:Intent)-[:BY]->(other:Agent)
WHERE other <> me
MERGE (me)-[:WAITS_ON]->(other)
"""

Q_DEADLOCK = """
MATCH p = (a:Agent)-[:WAITS_ON*2..8]->(a)
RETURN [x IN nodes(p) | x.id] AS cycle LIMIT 1
"""

Q_PATH = """
MATCH (from:Resource {id: $from}), (to:Resource {id: $to})
WITH shortestPath((from)-[:DEPENDS_ON*1..6]->(to)) AS p
RETURN CASE WHEN p IS NULL THEN NULL ELSE [x IN nodes(p) | x.name] END AS names
"""

Q_COLLISIONS = """
MATCH (n:Intent {id: $id})-[a:IMPACTS]->(r:Resource)<-[b:IMPACTS]-(g:Intent {status: 'granted'})
WHERE g <> n AND (
      (a.role = 'target' AND n.mode = 'exclusive')
   OR (b.role = 'target' AND g.mode = 'exclusive')
   OR (r.tier = 1 AND n.mode = 'exclusive' AND g.mode = 'exclusive'))
RETURN DISTINCT g.id AS other, r.id AS resource, r.tier AS tier
"""


# ---------------------------------------------------------------- intents

def _decide(intent_id, verdict, reason, policy=None, path=None, human=None, chain=None):
    write("""MATCH (n:Intent {id: $iid})
             CREATE (d:Decision {id: $id, at: $at, verdict: $verdict, reason: $reason, policy: $policy,
                                 path: $path, chain: $chain})-[:ABOUT]->(n)""",
          {"iid": intent_id, "id": new_id("dec"), "at": clock.now(), "verdict": verdict, "reason": reason,
           "policy": policy, "path": path or [], "chain": chain or []})
    if human:
        write("""MATCH (d:Decision)-[:ABOUT]->(:Intent {id: $iid}), (h:Human {id: $h})
                 WITH d, h ORDER BY d.at DESC LIMIT 1 MERGE (d)-[:AUTHORIZED_BY]->(h)""", {"iid": intent_id, "h": human})


def path_between(a, b):
    if a == b:
        return [a.split("@")[0]]
    r = rows(Q_PATH, {"from": a, "to": b})
    return r[0]["names"] if r and r[0]["names"] else [a.split("@")[0], b.split("@")[0]]


def request(agent_id, action, target, reason="", ttl=DEFAULT_TTL, priority=None, enforce=True):
    """An agent asks to change one resource. Returns granted / waiting / needs_approval / denied,
    with the reason and, when blocked, the dependency path to the other agent's change."""
    if action not in ACTIONS:
        return {"status": "denied", "reason": f"Unknown action {action}"}
    mode, depth, destructive = ACTIONS[action]
    with _lock:
        res = rows("MATCH (r:Resource {id: $t}) RETURN r.kind AS kind, r.tier AS tier, r.env AS env, r.name AS name",
                   {"t": target})
        agent = rows("MATCH (a:Agent {id: $a}) RETURN a.priority AS priority, a.name AS name", {"a": agent_id})
        if not res or not agent:
            return {"status": "denied", "reason": "Unknown agent or resource"}
        res, agent = res[0], agent[0]
        if res["kind"] not in VALID_TARGETS[action]:
            return {"status": "denied", "reason": f"Can't {action} a {res['kind']}"}
        prio = agent["priority"] if priority is None else priority
        iid = new_id("int")
        now = clock.now()
        write("""MATCH (a:Agent {id: $agent}), (t:Resource {id: $target})
                 CREATE (n:Intent {id: $id, action: $action, mode: $mode, target: $target, reason: $reason,
                                   priority: $prio, status: 'new', created_at: $now, ttl: $ttl,
                                   preemptible: $preemptible, destructive: $destructive, enforced: $enforce})-[:BY]->(a)
                 CREATE (n)-[:IMPACTS {role: 'target', hops: 0}]->(t)""",
              {"agent": agent_id, "target": target, "id": iid, "action": action, "mode": mode, "reason": reason,
               "prio": prio, "now": now, "ttl": ttl, "preemptible": action in ("deploy", "scale", "flip_flag"),
               "destructive": destructive, "enforce": enforce})
        zone = rows(Q_ZONE.format(depth=max(depth, 1)), {"target": target})[0]["zone"] if depth else []
        if zone:
            write("""MATCH (n:Intent {id: $id}) UNWIND $zone AS z MATCH (r:Resource {id: z.id})
                     CREATE (n)-[:IMPACTS {role: 'zone', hops: z.hops}]->(r)""", {"id": iid, "zone": zone})
        base = {"intent": iid, "action": action, "target": target, "zone": [z["id"] for z in zone],
                "zone_tier1": sum(1 for z in zone if z["tier"] == 1)}
        if not enforce:
            write("MATCH (n:Intent {id: $id}) SET n.status = 'granted', n.granted_at = $now, n.expires_at = $now + n.ttl",
                  {"id": iid, "now": now})
            hits = rows(Q_COLLISIONS, {"id": iid})
            for h in hits:
                write("""MATCH (a:Intent {id: $a}), (b:Intent {id: $b}), (r:Resource {id: $r})
                         CREATE (c:Collision {id: $cid, at: $at, tier: $tier})
                         CREATE (c)-[:BETWEEN]->(a), (c)-[:BETWEEN]->(b), (c)-[:AT]->(r)""",
                      {"a": iid, "b": h["other"], "r": h["resource"], "cid": new_id("col"), "at": now, "tier": h["tier"]})
            return base | {"status": "granted", "unchecked": True, "collisions": hits}

        # 1. environment scope: the "wrong database" incident
        env = rows(Q_ENV_SCOPE, {"agent": agent_id, "target": target})[0]
        if not env["allowed"]:
            why = f"{agent['name']} is scoped to {', '.join(env['scope'])}, and {res['name']} is in {env['env']}."
            return _deny(iid, base, why, "environment")
        # 2. authority: some human who may change this team's resources must sit at the top of the agent chain
        auth = rows(Q_AUTHORITY, {"agent": agent_id, "target": target})
        if not auth or not auth[0]["human"]:
            team = auth[0]["team"] if auth else "its owner"
            return _deny(iid, base, f"No human who can change {team}'s resources authorised this agent chain.", "authority")
        auth = auth[0]
        # 3. freeze windows are enforced here, not left in the agent's prompt
        fr = rows(Q_FREEZE, {"target": target})
        if fr and fr[0]["freeze"] and prio > 0:
            return _deny(iid, base, f"{fr[0]['reason']} is in force for {env['env']}. Only incident fixes go through, with approval.",
                         "freeze", chain=auth["chain"])
        # 4. big or destructive changes wait for a human
        money_migration = action == "migrate" and res["env"] == "prod" and res["tier"] == 1
        needs_human = ((destructive and res["env"] == "prod") or money_migration or len(zone) > APPROVAL_ZONE_SIZE
                       or (fr and fr[0]["freeze"]))
        if needs_human:
            why = ("Deleting a production resource" if destructive and res["env"] == "prod"
                   else f"Migrating {res['name']}, a production database on the money path" if money_migration
                   else "Change during a freeze" if fr and fr[0]["freeze"]
                   else f"This change ripples into {len(zone)} resources")
            write("MATCH (n:Intent {id: $id}) SET n.status = 'needs_approval', n.approver = $h", {"id": iid, "h": auth["human"]})
            _decide(iid, "needs_approval", f"{why}, so {auth['human_name']} has to approve it.", "approval", chain=auth["chain"])
            return base | {"status": "needs_approval", "reason": f"{why}. Waiting for {auth['human_name']}.",
                           "approver": auth["human"], "chain": auth["chain"]}
        return _grant(iid, base, auth)


def _deny(iid, base, why, policy, chain=None):
    write("MATCH (n:Intent {id: $id}) SET n.status = 'denied'", {"id": iid})
    _decide(iid, "denied", why, policy, chain=chain)
    return base | {"status": "denied", "reason": why, "policy": policy}


def _explain_blockers(iid, meets):
    """Turn 'where we meet' into readable dependency paths."""
    mine = rows("MATCH (n:Intent {id: $id}) RETURN n.target AS t", {"id": iid})[0]["t"]
    out = []
    for m in meets:
        if not m["intent"]:
            continue
        other = rows("""MATCH (g:Intent {id: $g})-[:BY]->(a:Agent)
                        RETURN g.target AS t, g.action AS action, a.id AS agent, a.name AS name""", {"g": m["intent"]})[0]
        p1 = path_between(m["resource"], mine)        # meeting point depends on my target
        p2 = path_between(m["resource"], other["t"])  # ...and on theirs
        mine_leg, their_leg = list(reversed(p1)), list(reversed(p2))
        out.append({"intent": m["intent"], "agent": other["agent"], "agent_name": other["name"],
                    "action": other["action"], "target": other["t"], "meet": m["resource"],
                    "mine": mine_leg, "theirs": their_leg,
                    "path": mine_leg + (list(reversed(their_leg[:-1])) if len(their_leg) > 1 else [])})
    return out


def _grant(iid, base, auth=None):
    r = rows(Q_TRY_GRANT, {"id": iid, "now": clock.now()}, read_only=False)[0]
    if r["status"] == "granted":
        why = f"No other change touches {base['target'].split('@')[0]} or the {len(base['zone'])} resources it affects."
        _decide(iid, "granted", why, path=[], human=auth["human"] if auth else None, chain=auth["chain"] if auth else None)
        return base | {"status": "granted", "reason": why, "chain": auth["chain"] if auth else []}
    rows(Q_AGENT_WAITS, {"id": iid}, read_only=False)
    blocked = _explain_blockers(iid, r["meets"])
    prio = rows("MATCH (n:Intent {id: $id}) RETURN n.priority AS p", {"id": iid})[0]["p"]
    if prio == 0:
        pre = preempt(iid)
        if pre:
            again = rows(Q_TRY_GRANT, {"id": iid, "now": clock.now()}, read_only=False)[0]
            if again["status"] == "granted":
                why = f"Incident fix: paused {', '.join(pre)} to make room."
                _decide(iid, "granted", why, "preemption", human=auth["human"] if auth else None)
                return base | {"status": "granted", "reason": why, "preempted": pre}
    b = blocked[0] if blocked else None
    def legs(b):
        a = " → ".join(b["mine"])
        t = " → ".join(b["theirs"]) if len(b["theirs"]) > 1 else ""
        return a + (f", and {t}" if t else "")
    def sentence(b):
        who = f"{b['agent_name']} ({b['action']} {b['target'].split('@')[0]})"
        meet = b['meet'].split('@')[0]
        if b['meet'] == b['target'] and len(b['mine']) <= 1:
            return f"Waits for {who}: it is already changing {meet}."
        return f"Waits for {who}. Both changes reach {meet}: {legs(b)}."
    why = sentence(b) if b else "Waiting for another change."
    _decide(iid, "waiting", why, "conflict", path=b["path"] if b else [])
    dl = resolve_deadlock()
    return base | {"status": "waiting", "reason": why, "blocked_by": blocked, "deadlock": dl}


def approve(iid, human_id, yes=True):
    with _lock:
        n = rows("MATCH (n:Intent {id: $id}) RETURN n.status AS s, n.target AS t, n.action AS a", {"id": iid})
        if not n or n[0]["s"] != "needs_approval":
            return {"status": "noop"}
        h = rows("MATCH (h:Human {id: $h}) RETURN h.name AS name", {"h": human_id})[0]
        if not yes:
            write("MATCH (n:Intent {id: $id}) SET n.status = 'denied'", {"id": iid})
            _decide(iid, "denied", f"{h['name']} rejected it.", "approval", human=human_id)
            return {"status": "denied"}
        _decide(iid, "approved", f"{h['name']} approved it.", "approval", human=human_id)
        base = {"intent": iid, "action": n[0]["a"], "target": n[0]["t"],
                "zone": [z["id"] for z in rows("MATCH (:Intent {id: $id})-[:IMPACTS {role: 'zone'}]->(r) RETURN r.id AS id", {"id": iid})]}
        return _grant(iid, base, {"human": human_id, "chain": [h["name"]]})


def release(iid, outcome="done"):
    """A change finished (or was abandoned). Free its locks and wake whoever was waiting on it."""
    with _lock:
        write("""MATCH (n:Intent {id: $id}) SET n.status = $o, n.finished_at = $now
                 WITH n OPTIONAL MATCH (n)-[w:WAITS_FOR]-() DELETE w""", {"id": iid, "o": outcome, "now": clock.now()})
        _refresh_agent_waits()
        return wake()


def _refresh_agent_waits():
    write("MATCH ()-[w:WAITS_ON]->() DELETE w")
    write("""MATCH (n:Intent {status: 'waiting'})-[:BY]->(me:Agent), (n)-[:WAITS_FOR]->(g:Intent {status: 'granted'})-[:BY]->(o:Agent)
             WHERE o <> me MERGE (me)-[:WAITS_ON]->(o)""")


def wake():
    """Retry waiting intents whose blockers are gone, most urgent first."""
    woken = []
    for w in rows("""MATCH (n:Intent {status: 'waiting'})
                     WHERE NOT (n)-[:WAITS_FOR]->(:Intent {status: 'granted'})
                     RETURN n.id AS id ORDER BY n.priority, n.created_at"""):
        write("MATCH (n:Intent {id: $id})-[w:WAITS_FOR]->() DELETE w", {"id": w["id"]})
        r = rows(Q_TRY_GRANT, {"id": w["id"], "now": clock.now()}, read_only=False)[0]
        if r["status"] == "granted":
            _decide(w["id"], "granted", "The change it was waiting for finished.")
            woken.append(w["id"])
        else:
            rows(Q_AGENT_WAITS, {"id": w["id"]}, read_only=False)
    return woken


def heartbeat(iid, ttl=DEFAULT_TTL):
    write("MATCH (n:Intent {id: $id, status: 'granted'}) SET n.expires_at = $exp", {"id": iid, "exp": clock.now() + ttl})


def sweep():
    """Leases whose agent stopped renewing them (crashed, hung) lapse, so nobody waits forever."""
    with _lock:
        lapsed = rows("""MATCH (n:Intent {status: 'granted'}) WHERE n.expires_at < $now RETURN n.id AS id""",
                      {"now": clock.now()})
        for l in lapsed:
            _decide(l["id"], "expired", "Its agent stopped renewing the lease, so the lock lapsed.", "lease")
            release(l["id"], "expired")
        return [l["id"] for l in lapsed]


def preempt(iid):
    """An incident fix (priority 0) may pause lower-priority changes that can be paused."""
    victims = rows("""MATCH (n:Intent {id: $id})-[:WAITS_FOR]->(g:Intent {status: 'granted'})-[:BY]->(a:Agent)
                      WHERE g.preemptible AND g.priority > n.priority
                      RETURN g.id AS id, a.name AS agent, g.action AS action, g.target AS target""", {"id": iid})
    for v in victims:
        _decide(v["id"], "preempted", "Paused so an incident fix could go first.", "preemption")
        write("""MATCH (g:Intent {id: $id}) SET g.status = 'preempted', g.finished_at = $now
                 WITH g OPTIONAL MATCH (g)-[w:WAITS_FOR]-() DELETE w""", {"id": v["id"], "now": clock.now()})
        write("MATCH (n:Intent {id: $n})-[w:WAITS_FOR]->(g:Intent {id: $g}) DELETE w", {"n": iid, "g": v["id"]})
    return [f"{v['agent']} ({v['action']} {v['target'].split('@')[0]})" for v in victims]


def resolve_deadlock():
    """Agents waiting on each other in a circle never finish. Find the cycle in the waits-on graph
    and roll back the least urgent, youngest participant."""
    found = rows(Q_DEADLOCK)
    if not found:
        return None
    cycle = found[0]["cycle"]
    victim = rows("""MATCH (a:Agent)<-[:BY]-(n:Intent {status: 'waiting'}) WHERE a.id IN $ids
                     RETURN a.id AS agent, a.name AS name, n.id AS intent
                     ORDER BY a.priority DESC, n.created_at DESC LIMIT 1""", {"ids": cycle})
    if not victim:
        return None
    v = victim[0]
    names = [r["name"] for r in rows("MATCH (a:Agent) WHERE a.id IN $ids RETURN a.id AS id, a.name AS name", {"ids": cycle})]
    why = f"Deadlock: {' → '.join(cycle)}. Rolled back {v['name']}'s waiting change so the others can finish."
    _decide(v["intent"], "aborted", why, "deadlock", path=cycle)
    write("""MATCH (n:Intent {id: $id}) SET n.status = 'aborted', n.finished_at = $now
             WITH n OPTIONAL MATCH (n)-[w:WAITS_FOR]-() DELETE w""", {"id": v["intent"], "now": clock.now()})
    held = rows("""MATCH (n:Intent {status: 'granted'})-[:BY]->(:Agent {id: $a}) RETURN n.id AS id""", {"a": v["agent"]})
    for h in held:
        _decide(h["id"], "aborted", "Rolled back to break a deadlock.", "deadlock", path=cycle)
        write("MATCH (n:Intent {id: $id}) SET n.status = 'aborted', n.finished_at = $now", {"id": h["id"], "now": clock.now()})
    _refresh_agent_waits()
    wake()
    return {"cycle": cycle, "names": names, "victim": v["agent"], "rolled_back": [v["intent"]] + [h["id"] for h in held]}


# ---------------------------------------------------------------- reading the state

def why(iid):
    """Everything about one intent: what it targets, what it affects, every decision, who authorised it."""
    n = rows("""MATCH (n:Intent {id: $id})-[:BY]->(a:Agent)
                RETURN n.id AS id, n.action AS action, n.target AS target, n.status AS status, n.reason AS reason,
                       n.priority AS priority, a.id AS agent, a.name AS agent_name""", {"id": iid})
    if not n:
        return {"error": "unknown intent"}
    out = n[0]
    out["zone"] = rows("""MATCH (:Intent {id: $id})-[i:IMPACTS {role: 'zone'}]->(r:Resource)
                          RETURN r.id AS id, r.tier AS tier, i.hops AS hops ORDER BY i.hops, r.tier""", {"id": iid})
    out["decisions"] = rows("""MATCH (d:Decision)-[:ABOUT]->(:Intent {id: $id})
                               OPTIONAL MATCH (d)-[:AUTHORIZED_BY]->(h:Human)
                               RETURN d.at AS at, d.verdict AS verdict, d.reason AS reason, d.policy AS policy,
                                      d.path AS path, d.chain AS chain, h.name AS human ORDER BY d.at""", {"id": iid})
    out["waits_for"] = rows("""MATCH (:Intent {id: $id})-[:WAITS_FOR]->(g:Intent)-[:BY]->(a:Agent)
                               RETURN g.id AS intent, g.action AS action, g.target AS target, a.name AS agent""", {"id": iid})
    out["authority"] = rows(Q_AUTHORITY, {"agent": out["agent"], "target": out["target"]})
    return out


def snapshot():
    """The live state the UI draws: active intents, what they lock, who waits on whom."""
    active = rows("""MATCH (n:Intent)-[:BY]->(a:Agent)
                     WHERE n.status IN ['granted', 'waiting', 'needs_approval']
                     RETURN n.id AS id, n.action AS action, n.target AS target, n.status AS status, n.mode AS mode,
                            n.priority AS priority, a.id AS agent, a.name AS agent_name, n.enforced AS enforced,
                            [(n)-[i:IMPACTS {role: 'zone'}]->(r) | r.id] AS zone,
                            [(n)-[:WAITS_FOR]->(g) | g.id] AS waits_for""")
    waits = rows("MATCH (a:Agent)-[:WAITS_ON]->(b:Agent) RETURN a.id AS a, b.id AS b")
    return {"now": clock.now(), "active": active, "agent_waits": waits}


Q_AGENTS = """
MATCH (a:Agent)
OPTIONAL MATCH (h:Human)-[:AUTHORIZED]->(top:Agent)-[d:DELEGATED_TO*0..3]->(a)
WITH a, h, d ORDER BY length(d)
WITH a, collect(CASE WHEN h IS NULL THEN NULL ELSE [h.name] + [x IN nodes(d) | x.name] END) AS chains
RETURN a.id AS id, a.name AS name, a.role AS role, a.priority AS priority, a.kind AS kind,
       [(a)-[:SCOPED_TO]->(e:Env) | e.name] AS envs, chains[0] AS chain,
       [(n:Intent)-[:BY]->(a) WHERE n.status IN ['granted', 'waiting', 'needs_approval'] |
          {id: n.id, action: n.action, target: n.target, status: n.status}] AS active,
       size([(n:Intent)-[:BY]->(a) WHERE n.status = 'done' | 1]) AS done,
       size([(n:Intent)-[:BY]->(a) WHERE n.status = 'denied' | 1]) AS denied
ORDER BY a.priority, a.id
"""


def agents():
    return rows(Q_AGENTS)


def policies():
    fr = rows("MATCH (f:Freeze {id: 'prod-freeze'}) RETURN f.active AS on, f.reason AS reason")[0]
    return [
        {"name": "Impact-aware locking", "on": True,
         "rule": "A change locks its target and everything that depends on it, up to 1-3 hops depending on the action. "
                 "Two changes conflict when one's target is inside the other's reach, or when both reach the same customer-facing service."},
        {"name": "Atomic grant", "on": True,
         "rule": "The conflict check and the grant run as one Cypher query, so two agents can never be granted overlapping changes."},
        {"name": "Environment scope", "on": True, "rule": "An agent may only touch the environments it is scoped to. A staging agent can't reach prod."},
        {"name": "Human authority", "on": True,
         "rule": "Every agent must trace back, through any chain of sub-agents, to a human who may change the owning team's resources."},
        {"name": "Code freeze", "on": bool(fr["on"]), "rule": f"{fr['reason']}: production changes are blocked; incident fixes need approval."},
        {"name": "Sign-off for risky changes", "on": True,
         "rule": f"Deletes in prod, migrations of customer-facing databases, and changes that reach more than {APPROVAL_ZONE_SIZE} resources wait for a human."},
        {"name": "Incident priority", "on": True, "rule": "A priority-0 incident fix pauses lower-priority changes that can be paused, and goes first."},
        {"name": "Deadlock breaking", "on": True,
         "rule": "Agents waiting on each other in a circle are found as a cycle in the waits-on graph. The least urgent one is rolled back."},
        {"name": "Leases", "on": True, "rule": f"Locks lapse after {DEFAULT_TTL:.0f}s without a heartbeat, so a crashed agent can't block everyone."},
    ]


def topology(env="prod"):
    nodes = rows("""MATCH (r:Resource {env: $env}) RETURN r.id AS id, r.name AS name, r.kind AS kind, r.tier AS tier,
                    r.team AS team""", {"env": env})
    edges = rows("""MATCH (a:Resource {env: $env})-[d:DEPENDS_ON]->(b:Resource) RETURN a.id AS a, b.id AS b, d.kind AS kind""",
                 {"env": env})
    return {"nodes": nodes, "edges": edges}


def stats():
    return {
        "nodes": rows("MATCH (n) RETURN labels(n)[0] AS label, count(n) AS count ORDER BY count DESC"),
        "edges": rows("MATCH ()-[r]->() RETURN type(r) AS type, count(r) AS count ORDER BY count DESC"),
    }


if __name__ == "__main__":
    build()
    print(stats())
