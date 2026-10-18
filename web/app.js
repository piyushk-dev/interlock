/* Interlock console. The map, tables and feed are all drawn from the live FalkorDB state. */
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const short = (id) => String(id || "").split("@")[0];
const envOf = (id) => String(id || "").split("@")[1] || "prod";
const api = async (path, body) => {
  const r = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
  return r.json();
};
const HUMANS = { arjun: "Arjun (Platform on-call)", priya: "Priya (Checkout lead)", neha: "Neha (Catalog lead)", rahul: "Rahul (Fulfilment lead)" };
const VERDICT = { granted: "Granted", waiting: "Waiting", denied: "Blocked", needs_approval: "Needs approval", approved: "Approved", rejected: "Rejected",
  collision: "Collision", aborted: "Rolled back", deadlock: "Deadlock", expired: "Lease lapsed", crashed: "Crashed", preempted: "Paused",
  unchecked: "Unchecked", done: "Released" };
const ACTION = { deploy: "deploy", restart: "restart", migrate: "migrate", rotate_secret: "rotate secrets on", flip_flag: "flip", scale: "scale", diagnose: "diagnose", delete: "delete" };
const S = { topo: null, pos: {}, state: null, seq: 0, clash: {}, flash: {}, filter: "all", view: "map", events: [] };
let W = 1000, H = 600;

/* ------------------------------------------------------------------ layout */
function layout(topo) {
  const nodes = topo.nodes, byId = Object.fromEntries(nodes.map((n) => [n.id, n]));
  const out = {}, inc = {};
  nodes.forEach((n) => { out[n.id] = []; inc[n.id] = []; });
  topo.edges.forEach((e) => { out[e.a].push(e.b); inc[e.b].push(e.a); });
  const rank = {};
  const visit = (id, seen = new Set()) => {
    if (rank[id] !== undefined) return rank[id];
    if (seen.has(id)) return 0;
    seen.add(id);
    rank[id] = inc[id].length ? Math.max(...inc[id].map((p) => visit(p, seen) + 1)) : 0;
    return rank[id];
  };
  nodes.forEach((n) => visit(n.id));
  const svcMax = Math.max(...nodes.filter((n) => n.kind === "service" || n.kind === "topic").map((n) => rank[n.id]));
  nodes.forEach((n) => { if (n.kind === "datastore" || n.kind === "flag") rank[n.id] = svcMax + 1; });
  const cols = [];
  nodes.forEach((n) => { (cols[rank[n.id]] = cols[rank[n.id]] || []).push(n.id); });
  const teams = ["Growth", "Platform", "Identity", "Catalog", "Checkout", "Payments", "Fulfilment"];
  cols.forEach((c) => c.sort((a, b) => teams.indexOf(byId[a].team) - teams.indexOf(byId[b].team) || a.localeCompare(b)));
  const norm = () => { const m = {}; cols.forEach((c) => c.forEach((id, i) => { m[id] = i / Math.max(1, c.length - 1); })); return m; };
  for (let sweep = 0; sweep < 8; sweep++) {
    const m = norm();
    const order = sweep % 2 ? [...cols.keys()].reverse() : [...cols.keys()];
    order.forEach((ci) => {
      const bary = (id) => {
        const nb = [...out[id], ...inc[id]].filter((x) => rank[x] !== ci && Math.abs(rank[x] - ci) <= 2);
        return nb.length ? nb.reduce((s, x) => s + m[x], 0) / nb.length : m[id];
      };
      cols[ci] = cols[ci].map((id) => [id, byId[id].kind === "flag" ? 2 + m[id] : bary(id)]).sort((a, b) => a[1] - b[1]).map((k) => k[0]);
    });
  }
  // columns are as wide as their widest card; the spare width becomes the gaps the edges run through
  const cw = cols.map((c) => Math.max(...c.map((id) => byId[id].w)));
  const pad = 6;
  S.rawGap = (W - 2 * pad - cw.reduce((s, w) => s + w, 0)) / Math.max(1, cols.length - 1);
  const gap = Math.max(18, S.rawGap);
  const pos = {};
  let left = pad;
  cols.forEach((c, ci) => {
    const step = Math.min(84, (H - 40) / Math.max(1, c.length));
    const top = (H - step * (c.length - 1)) / 2 + 6;
    c.forEach((id, i) => { pos[id] = { x: left + byId[id].w / 2, y: top + i * step, n: byId[id], w: byId[id].w }; });
    left += cw[ci] + gap;
  });
  return pos;
}

const CARD_H = 26;
let measureCtx;
function cardWidth(name, fs) {
  measureCtx = measureCtx || document.createElement("canvas").getContext("2d");
  measureCtx.font = `500 ${fs}px "IBM Plex Sans"`;
  return Math.ceil(measureCtx.measureText(name).width) + 36;
}

function edgePath(a, b) {
  const A = S.pos[a], B = S.pos[b];
  const x1 = A.x + A.w / 2, x2 = B.x - B.w / 2, y1 = A.y, y2 = B.y;
  if (x2 <= x1 + 6) {  // same column: bow out to the right
    const xr = Math.max(x1, B.x + B.w / 2) + 26;
    return `M${x1} ${y1} C ${xr} ${y1}, ${xr} ${y2}, ${B.x + B.w / 2} ${y2}`;
  }
  const dx = Math.max(24, (x2 - x1) * 0.5);
  return `M${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
}

// small glyph on the left of each card: what kind of thing it is
function glyph(g, kind, x) {
  const k = g.append("g").attr("class", "glyph").attr("transform", `translate(${x},0)`);
  if (kind === "datastore") {
    k.append("ellipse").attr("cx", 0).attr("cy", -3.5).attr("rx", 4.5).attr("ry", 1.8);
    k.append("path").attr("d", "M-4.5 -3.5 V3.5 A4.5 1.8 0 0 0 4.5 3.5 V-3.5");
  } else if (kind === "topic") {
    k.append("path").attr("d", "M-4.5 -3 H4.5 M-4.5 0 H4.5 M-4.5 3 H2");
  } else if (kind === "flag") {
    k.append("path").attr("d", "M-3.5 5 V-5 H4 L2 -2 L4 1 H-3.5");
  } else {
    k.append("rect").attr("x", -4.5).attr("y", -4.5).attr("width", 9).attr("height", 9).attr("rx", 2);
  }
}

async function drawMap() {
  if (!S.topo) S.topo = await api("/api/topology?env=prod");
  const box = $("#mimic");
  if (!box.clientWidth) return;
  W = Math.max(680, box.clientWidth); H = Math.max(380, box.clientHeight);
  // largest label size that still leaves room between columns for the edges
  let fs;
  for (fs of [12, 11.5, 11, 10.5, 10]) {
    S.topo.nodes.forEach((n) => { n.w = cardWidth(n.name, fs); });
    S.pos = layout(S.topo);
    if (S.rawGap >= 30) break;
  }
  // still too tight (phones): draw wider and let the SVG scale down
  while (S.rawGap < 24) { W += 40; S.pos = layout(S.topo); }
  d3.select("#mimic").selectAll("*").remove();
  const svg = d3.select("#mimic").append("svg").attr("viewBox", `0 0 ${W} ${H}`).attr("preserveAspectRatio", "xMidYMid meet")
    .attr("role", "img").attr("aria-label", "Dependency map of production, coloured by what agents are changing");
  S.edges = S.topo.edges.map((e) => ({ ...e, d: edgePath(e.a, e.b) }));
  S.edgeSel = svg.append("g").selectAll("path").data(S.edges).join("path").attr("class", "edge").attr("d", (e) => e.d);
  const embed = document.body.classList.contains("embed");
  S.nodeSel = svg.append("g").selectAll("g.node").data(Object.values(S.pos)).join("g").attr("class", "node")
    .attr("transform", (p) => `translate(${p.x},${p.y})`).attr("tabindex", embed ? -1 : 0)
    .on("mouseenter", (ev, p) => S.edgeSel.classed("near", (e) => e.a === p.n.id || e.b === p.n.id))
    .on("mouseleave", () => S.edgeSel.classed("near", false))
    .on("click", (ev, p) => { if (!embed) openResource(p.n.id); })
    .on("keydown", (ev, p) => { if (ev.key === "Enter" && !embed) openResource(p.n.id); });
  S.nodeSel.each(function (p) {
    const g = d3.select(this), w = p.w, h = CARD_H;
    g.append("rect").attr("class", "ring").attr("x", -w / 2).attr("y", -h / 2).attr("width", w).attr("height", h).attr("rx", 7);
    g.append("rect").attr("class", "card").attr("x", -w / 2).attr("y", -h / 2).attr("width", w).attr("height", h).attr("rx", 7);
    glyph(g, p.n.kind, -w / 2 + 14);
    g.append("text").attr("class", "lbl").attr("x", -w / 2 + 25).attr("y", 4.2).style("font-size", `${fs}px`).text(p.n.name);
    if (p.n.tier === 1) g.append("circle").attr("class", "t1").attr("cx", w / 2 - 1).attr("cy", -h / 2 + 1).attr("r", 3.4);
    g.append("title").text(`${p.n.name}: ${p.n.kind}, ${p.n.team}${p.n.tier === 1 ? ", customer-facing" : ""}`);
  });
  S.gHold = svg.append("g").attr("class", "tags");
  paint();
}

/* ------------------------------------------------------------------ paint live state onto the map */
function paint() {
  const st = S.state;
  if (!st || !S.nodeSel) return;
  const now = Date.now();
  const held = {}, zone = new Set(), waiting = new Set(), approval = new Set(), routes = [], heldSet = new Set(), who = {};
  for (const i of st.snapshot.active) {
    if (envOf(i.target) !== "prod") continue;
    (who[i.target] = who[i.target] || []).push(i);
    if (i.status === "granted") {
      (held[i.target] = held[i.target] || []).push(i.agent_name);
      heldSet.add(i.target);
      i.zone.forEach((z) => zone.add(z));
      routes.push(new Set([i.target, ...i.zone]));
    } else if (i.status === "waiting") waiting.add(i.target);
    else if (i.status === "needs_approval") approval.add(i.target);
  }
  for (const k of Object.keys(S.clash)) if (S.clash[k] < now) delete S.clash[k];
  for (const k of Object.keys(S.flash)) if (S.flash[k] < now) delete S.flash[k];
  S.nodeSel.attr("class", (p) => {
    const id = p.n.id;
    if (S.clash[id]) return "node clash";
    if (held[id]) return "node held";
    if (approval.has(id)) return "node approval";
    if (waiting.has(id)) return "node waiting";
    if (zone.has(id)) return "node zone";
    return "node";
  });
  S.edgeSel.attr("class", (e) => {
    if (S.clash[e.a] && S.clash[e.b]) return "edge clash";
    for (const r of routes) if (r.has(e.a) && r.has(e.b)) return heldSet.has(e.a) || heldSet.has(e.b) ? "edge route held" : "edge route";
    return "edge";
  });
  // one tag per resource above its card: the agent holding it, else the one asking for it
  const rankOf = { granted: 0, needs_approval: 1, waiting: 2 };
  const tags = Object.entries(who).filter(([id]) => S.pos[id]).map(([id, list]) => {
    list.sort((a, b) => (rankOf[a.status] ?? 3) - (rankOf[b.status] ?? 3));
    const top = list[0], more = list.length - 1;
    return { id, status: top.status, text: top.agent_name + (more ? ` +${more}` : "") };
  });
  measureCtx.font = '500 10.5px "IBM Plex Mono"';
  S.gHold.selectAll("g.tag").data(tags, (d) => d.id).join((en) => {
    const g = en.append("g");
    g.append("rect").attr("height", 17).attr("rx", 4);
    g.append("circle").attr("r", 2.6).attr("cy", 0);
    g.append("text").attr("y", 3.6);
    return g;
  }).attr("class", (d) => `tag ${d.status}`).each(function (d) {
    const p = S.pos[d.id], w = Math.ceil(measureCtx.measureText(d.text).width) + 22;
    const g = d3.select(this).attr("transform", `translate(${p.x - p.w / 2},${p.y - CARD_H / 2 - 12})`);
    g.select("rect").attr("x", 0).attr("y", -8.5).attr("width", w);
    g.select("circle").attr("cx", 8);
    g.select("text").attr("x", 15).text(d.text);
  });
}

/* ------------------------------------------------------------------ stats, approvals, tables */
function fmt(n) { return Math.round(n).toLocaleString("en-IN"); }

function renderStats() {
  const st = S.state, on = st.metrics.on, off = st.metrics.off;
  const avg = on.waits ? on.wait_time / on.waits : 0;
  const cells = [
    ["Changes completed", fmt(on.completed + off.completed)],
    ["Collisions prevented", fmt(on.prevented)],
    ["Blocked by policy", fmt(on.denied)],
    ["Deadlocks resolved", fmt(on.deadlocks)],
    ["Average wait", `${avg.toFixed(1)}s`],
    ["Collisions, enforcement off", fmt(off.collisions), "off"],
    ["Outages, enforcement off", fmt(off.incidents), "off"],
  ];
  $("#stats").innerHTML = cells.map(([l, v, c]) => `<div class="stat ${c || ""}"><div class="v">${v}</div><div class="l">${l}</div></div>`).join("");
  const inflight = st.snapshot.active.filter((i) => i.status === "granted").length;
  const waits = st.snapshot.active.filter((i) => i.status === "waiting").length;
  $("#inflight").textContent = `${inflight} in flight, ${waits} waiting`;
  $("#enforce").checked = st.enforce;
  $("#enforceState").textContent = st.enforce ? "On" : "Off";
  $("#run").textContent = st.running ? "Pause fleet" : "Start fleet";
  $("#fleet").value = String(st.size);
  $("#speed").value = String(Math.round(st.speed));
  $("#scn-freeze")?.setAttribute("aria-pressed", String(st.freeze));
}

function renderApprovals() {
  const p = S.state.pending || [];
  $("#apprCount").textContent = p.length;
  $("#apprCount").classList.toggle("hot", p.length > 0);
  if (!p.length) { $("#approvals").innerHTML = `<p class="empty">Nothing waiting. Risky changes land here for a human.</p>`; return; }
  $("#approvals").innerHTML = p.slice(0, 3).map((x) => `
    <div class="approval">
      <p class="what"><span class="id">${esc(x.agent)}</span> wants to ${esc(ACTION[x.action] || x.action)} <span class="id">${esc(short(x.target))}</span></p>
      <p>${esc((x.reason || "").replace(/, so .* has to approve it\.?$/, "."))}</p>
      <div class="row">
        <button class="btn small ok" data-ok="${esc(x.intent)}" data-h="${esc(x.approver || "arjun")}">Approve</button>
        <button class="btn small secondary" data-no="${esc(x.intent)}" data-h="${esc(x.approver || "arjun")}">Reject</button>
        <span class="as">as ${esc(HUMANS[x.approver] || "Arjun")}</span>
      </div>
    </div>`).join("") + (p.length > 3 ? `<p class="empty">${p.length - 3} more waiting.</p>` : "");
  document.querySelectorAll("[data-ok]").forEach((b) => b.onclick = async () => { await api("/api/approve", { intent: b.dataset.ok, human: b.dataset.h, yes: true }); poll(); });
  document.querySelectorAll("[data-no]").forEach((b) => b.onclick = async () => { await api("/api/approve", { intent: b.dataset.no, human: b.dataset.h, yes: false }); poll(); });
}

const SCENARIOS = [
  { id: "incident", title: "Outage", text: "Checkout fails. The incident agent restarts it and jumps the queue." },
  { id: "freeze", title: "Code freeze", text: "Freeze prod for a sale. Only approved incident fixes pass.", toggle: true },
  { id: "rogue", title: "Rogue agents", text: "A staging bot targets a prod database; a sub-agent overreaches." },
  { id: "crash", title: "Agent crash", text: "An agent dies holding a lock. The lease lapses." },
  { id: "deadlock", title: "Deadlock", text: "Two agents each wait for the other's resource." },
  { id: "migration", title: "Risky migration", text: "Re-shard payments-db. Needs a human to sign off." },
];

function buildScenarios() {
  $("#scenarios").innerHTML = SCENARIOS.map((s) => `<button type="button" class="scn" id="scn-${s.id}" ${s.toggle ? 'aria-pressed="false"' : ""}><b>${esc(s.title)}</b><span>${esc(s.text)}</span></button>`).join("");
  SCENARIOS.forEach((s) => $(`#scn-${s.id}`).addEventListener("click", async () => {
    if (s.toggle) {
      const on = $(`#scn-${s.id}`).getAttribute("aria-pressed") !== "true";
      await api("/api/scenario", { name: on ? "freeze_on" : "freeze_off" });
    } else {
      const r = await api("/api/scenario", { name: s.id });
      if (r && r.error) S.events.unshift({ seq: -1, at: S.state?.snapshot?.now || 0, kind: "system", text: r.error, verdict: null });
    }
    if (!S.state?.running) await api("/api/sim", { action: "start" });
    poll();
  }));
}

async function renderAgents() {
  const list = await api("/api/agents");
  const label = { working: "Changing", waiting: "Waiting", approval: "Awaiting approval", idle: "Idle", crashed: "Crashed", scripted: "Scripted" };
  $("#view-agents").innerHTML = `<table><thead><tr><th>Agent</th><th>State</th><th>Current change</th><th>Authorised by</th><th>Scope</th><th>Done</th></tr></thead><tbody>
    ${list.map((a) => {
      const cur = a.active[0];
      return `<tr class="${cur ? "click" : ""}" ${cur ? `data-i="${esc(cur.id)}"` : ""}>
        <td><span class="id">${esc(a.name)}</span><div class="sub">${esc(a.role)}${a.team ? `, ${esc(a.team)}` : ""} <span class="prio">P${a.priority}</span>${a.kind === "mcp" ? ", via MCP" : ""}</div></td>
        <td><span class="state"><span class="dot ${esc(a.state)}"></span>${esc(label[a.state] || a.state)}</span></td>
        <td>${cur ? `${esc(ACTION[cur.action] || cur.action)} <span class="id">${esc(short(cur.target))}</span>${envOf(cur.target) !== "prod" ? ' <span class="sub">(staging)</span>' : ""} <span class="badge ${esc(cur.status)}">${esc(VERDICT[cur.status])}</span>` : '<span class="sub">None</span>'}</td>
        <td>${a.chain ? `<span class="chain">${a.chain.map((c, i) => `<span class="step ${i === 0 ? "human" : ""}">${esc(c)}</span>`).join('<span class="sep">/</span>')}</span>` : '<span class="badge denied">No human</span>'}</td>
        <td>${esc(a.envs.join(", "))}</td>
        <td>${a.done}</td></tr>`;
    }).join("")}</tbody></table>`;
  document.querySelectorAll("#view-agents [data-i]").forEach((r) => r.addEventListener("click", () => openIntent(r.dataset.i)));
}

function renderChanges() {
  const act = [...S.state.snapshot.active].sort((a, b) => ["granted", "needs_approval", "waiting"].indexOf(a.status) - ["granted", "needs_approval", "waiting"].indexOf(b.status));
  $("#view-changes").innerHTML = `<table><thead><tr><th>Change</th><th>Agent</th><th>Status</th><th>Impact zone</th><th>Waiting for</th></tr></thead><tbody>
    ${act.length ? act.map((i) => `<tr class="click" data-i="${esc(i.id)}">
      <td>${esc(ACTION[i.action] || i.action)} <span class="id">${esc(short(i.target))}</span>${envOf(i.target) !== "prod" ? ' <span class="sub">(staging)</span>' : ""}</td>
      <td><span class="id">${esc(i.agent_name)}</span> <span class="prio">P${i.priority}</span></td>
      <td><span class="badge ${esc(i.status)}">${esc(VERDICT[i.status])}</span></td>
      <td>${i.zone.length} <span class="sub">${esc(i.zone.slice(0, 4).map(short).join(", "))}${i.zone.length > 4 ? "…" : ""}</span></td>
      <td>${i.waits_for.length ? esc(i.waits_for.length + " change" + (i.waits_for.length > 1 ? "s" : "")) : '<span class="sub">-</span>'}</td></tr>`).join("")
      : `<tr><td colspan="5" class="sub">No changes in flight. Start the fleet.</td></tr>`}</tbody></table>`;
  document.querySelectorAll("#view-changes [data-i]").forEach((r) => r.addEventListener("click", () => openIntent(r.dataset.i)));
}

async function renderPolicies() {
  const ps = await api("/api/policies");
  $("#view-policies").innerHTML = ps.map((p) => `<div class="policy"><b>${esc(p.name)}</b><span class="badge ${p.on ? "granted" : ""}">${p.on ? "Enforced" : "Off"}</span><p>${esc(p.rule)}</p></div>`).join("");
}

function setView(v) {
  S.view = v;
  document.querySelectorAll(".views button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.view === v)));
  document.querySelectorAll(".view").forEach((el) => { el.hidden = el.id !== `view-${v}`; });
  if (v === "map") drawMap();
  if (v === "agents") renderAgents();
  if (v === "changes") renderChanges();
  if (v === "policies") renderPolicies();
}

/* ------------------------------------------------------------------ decisions feed */
function fmtT(t) { const m = Math.floor(t / 60), s = Math.floor(t % 60); return `${m}:${String(s).padStart(2, "0")}`; }
function renderFeed(newSeqs = new Set()) {
  const f = S.filter;
  const keep = S.events.filter((e) => f === "all" || (f === "collision" ? e.kind === "collision" : f === "denied" ? e.verdict === "denied" : e.verdict === "waiting"));
  $("#rows").innerHTML = keep.slice(0, 120).map((e) => {
    const v = e.kind === "deadlock" ? "deadlock" : e.kind === "collision" ? "collision" : e.verdict;
    return `<li class="row-item ${newSeqs.has(e.seq) ? "fresh" : ""}" ${e.intent ? `data-i="${esc(e.intent)}" tabindex="0"` : ""}>
      <span class="t">${fmtT(e.at)}</span>
      <div><div class="top">${e.agent ? `<span class="id">${esc(e.agent)}</span>` : "<span>System</span>"}${v && VERDICT[v] ? `<span class="badge ${esc(v)}">${esc(VERDICT[v])}</span>` : ""}</div>
      <div class="txt">${esc(e.text)}</div></div></li>`;
  }).join("") || `<li class="row-item"><span></span><div class="txt">Nothing yet. Start the fleet.</div></li>`;
  document.querySelectorAll("#rows [data-i]").forEach((r) => {
    r.addEventListener("click", () => openIntent(r.dataset.i));
    r.addEventListener("keydown", (ev) => { if (ev.key === "Enter") openIntent(r.dataset.i); });
  });
}

/* ------------------------------------------------------------------ polling */
async function poll() {
  try {
    let [st, ev] = await Promise.all([api("/api/state"), api(`/api/events?after=${S.seq}`)]);
    if (st.seq < S.seq) {  // the fleet was reset elsewhere: start the feed over
      S.seq = 0; S.events = [];
      ev = await api("/api/events?after=0");
      renderFeed();
    }
    S.state = st;
    const fresh = new Set();
    for (const e of ev) {
      S.seq = Math.max(S.seq, e.seq);
      fresh.add(e.seq);
      S.events.unshift(e);
      const now = Date.now();
      if (e.kind === "collision" && e.extra) (e.extra.all || [e.extra.resource]).forEach((r) => { S.clash[r] = now + 4000; });
      if (e.verdict === "denied") {
        const m = e.text.match(/(?:delete|deploy|restart|migrate|flip_flag|rotate_secret|scale) ([\w-]+)/);
        if (m) S.flash[`${m[1]}@prod`] = now + 1800;
      }
    }
    S.events = S.events.slice(0, 400);
    renderStats();
    renderApprovals();
    if (ev.length) renderFeed(fresh);
    if (S.view === "map") paint();
    if (S.view === "changes") renderChanges();
    if (S.view === "agents" && (S.tick = (S.tick || 0) + 1) % 3 === 0) renderAgents();
  } catch (err) { console.error(err); }
}

/* ------------------------------------------------------------------ detail drawer */
function openSheet(head, body) {
  $("#sheet").innerHTML = `<header><div>${head}</div><button class="x" type="button" aria-label="Close">&times;</button></header><div class="content">${body}</div>`;
  $("#sheet").hidden = false; $("#scrim").hidden = false;
  $("#sheet .x").onclick = closeSheet;
  $("#sheet .x").focus();
}
function closeSheet() { $("#sheet").hidden = true; $("#scrim").hidden = true; }

async function openIntent(id) {
  if (document.body.classList.contains("embed")) return;
  const [w, cy] = await Promise.all([api(`/api/why?intent=${encodeURIComponent(id)}`), S.cypher || api("/api/cypher")]);
  S.cypher = cy;
  if (w.error) return;
  const a = (w.authority && w.authority[0]) || {};
  const chain = a.chain && a.chain.length ? `<span class="chain">${a.chain.map((c, i) => `<span class="step ${i === 0 ? "human" : ""}">${esc(c)}</span>`).join('<span class="sep">/</span>')}</span>` : '<span class="badge denied">No human authorised this chain</span>';
  const zone = w.zone.length ? `<div class="zone-list">${w.zone.map((z) => `<span class="${z.tier === 1 ? "t1" : ""}" title="${z.hops} hop${z.hops > 1 ? "s" : ""} away">${esc(short(z.id))}</span>`).join("")}</div>` : '<span class="sub">Only the target itself</span>';
  const q = (name) => (cy.find((c) => c.name === name) || {}).cypher || "";
  openSheet(
    `<h3><span class="id">${esc(w.agent_name)}</span> wants to ${esc(ACTION[w.action] || w.action)} <span class="id">${esc(short(w.target))}</span></h3>
     <div class="sub"><span class="badge ${esc(w.status)}">${esc(VERDICT[w.status] || w.status)}</span> ${envOf(w.target)}, priority ${w.priority}</div>`,
    `<dl class="kv">
       <dt>Authorised by</dt><dd>${chain}</dd>
       <dt>Owning team</dt><dd>${esc(a.team || "-")}</dd>
       ${w.waits_for.length ? `<dt>Waiting for</dt><dd>${w.waits_for.map((x) => `<span class="id">${esc(x.agent)}</span> (${esc(x.action)} <span class="id">${esc(short(x.target))}</span>)`).join(", ")}</dd>` : ""}
     </dl>
     <h4>Impact zone: ${w.zone.length} resource${w.zone.length === 1 ? "" : "s"}</h4>${zone}
     <h4>Decisions</h4>
     <ol class="timeline">${w.decisions.map((d) => `<li class="${esc(d.verdict)}"><span class="badge ${esc(d.verdict)}">${esc(VERDICT[d.verdict] || d.verdict)}</span> <span class="sub">${fmtT(d.at)}</span>
        <div class="why">${esc(d.reason)}</div>${d.human ? `<div class="signed">Signed by ${esc(d.human)}</div>` : ""}</li>`).join("")}</ol>
     <details><summary>The Cypher behind this decision</summary><pre>${esc(q("try_grant"))}</pre><pre>${esc(q("zone"))}</pre><pre>${esc(q("authority"))}</pre></details>`);
}

function openResource(id) {
  const p = S.pos[id];
  const here = (S.state?.snapshot.active || []).filter((i) => i.target === id || i.zone.includes(id));
  const uses = S.topo.edges.filter((e) => e.a === id).map((e) => short(e.b));
  const usedBy = S.topo.edges.filter((e) => e.b === id).map((e) => short(e.a));
  openSheet(
    `<h3><span class="id">${esc(p.n.name)}</span></h3><div class="sub">${esc(p.n.kind)} owned by ${esc(p.n.team)}${p.n.tier === 1 ? ", customer-facing" : ""}</div>`,
    `<dl class="kv"><dt>Depends on</dt><dd>${esc(uses.join(", ") || "Nothing")}</dd><dt>Used by</dt><dd>${esc(usedBy.join(", ") || "Nothing")}</dd></dl>
     <h4>Changes touching it now</h4>
     ${here.length ? `<table><tbody>${here.map((i) => `<tr class="click" data-i="${esc(i.id)}"><td><span class="id">${esc(i.agent_name)}</span><div class="sub">${esc(ACTION[i.action] || i.action)} ${esc(short(i.target))}${i.target === id ? "" : ", reaches it"}</div></td><td><span class="badge ${esc(i.status)}">${esc(VERDICT[i.status])}</span></td></tr>`).join("")}</tbody></table>` : '<p class="empty">None right now.</p>'}`);
  document.querySelectorAll("#sheet [data-i]").forEach((r) => r.addEventListener("click", () => openIntent(r.dataset.i)));
}

/* ------------------------------------------------------------------ boot */
async function boot() {
  const qs = new URLSearchParams(location.search);
  if (qs.has("embed")) {
    document.body.classList.add("embed");
    if (qs.has("feed")) document.body.classList.add("with-feed");
    await api("/api/ensure_running", {});
  }
  try { await document.fonts.ready; } catch (e) { /* measure with the fallback font */ }
  buildScenarios();
  document.querySelectorAll(".views button").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
  document.querySelectorAll("#feedTabs button").forEach((b) => b.addEventListener("click", () => {
    S.filter = b.dataset.f;
    document.querySelectorAll("#feedTabs button").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
    renderFeed();
  }));
  $("#enforce").addEventListener("change", async (e) => { S.state = await api("/api/sim", { action: "enforce", enforce: e.target.checked }); poll(); });
  $("#auto").addEventListener("change", async (e) => { await api("/api/sim", { action: "auto_approve", auto: e.target.checked }); });
  $("#fleet").addEventListener("change", async (e) => { S.events = []; S.seq = 0; await api("/api/sim", { action: "reset", agents: Number(e.target.value) }); renderFeed(); poll(); });
  $("#speed").addEventListener("change", async (e) => { await api("/api/sim", { action: "speed", speed: Number(e.target.value) }); poll(); });
  $("#run").addEventListener("click", async () => { await api("/api/sim", { action: S.state?.running ? "pause" : "start" }); poll(); });
  $("#reset").addEventListener("click", async () => { S.events = []; S.seq = 0; await api("/api/sim", { action: "reset" }); renderFeed(); poll(); });
  $("#scrim").addEventListener("click", closeSheet);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSheet(); });
  let rt;
  window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => { if (S.view === "map") drawMap(); }, 200); });
  await poll();
  await drawMap();
  renderFeed();
  setInterval(poll, 800);
}
boot();
