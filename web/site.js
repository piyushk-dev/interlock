/* Product page: live numbers, the collision explainer, runnable scenarios and the how-it-works tabs. */
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const api = async (path, body) => (await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {})).json();

/* ------------------------------------------------------------------ live numbers under the hero map */
let STATE = null;
async function readout() {
  try {
    const s = STATE = await api("/api/state");
    const a = s.snapshot.active;
    $("#r-flight").textContent = a.filter((i) => i.status === "granted").length;
    $("#r-wait").textContent = a.filter((i) => i.status === "waiting").length;
    $("#r-prev").textContent = s.metrics.on.prevented;
    $("#r-block").textContent = s.metrics.on.denied;
    $("#r-done").textContent = s.metrics.on.completed + s.metrics.off.completed;
    renderScenarioActions();
  } catch (e) { /* the map still runs */ }
}
readout(); setInterval(readout, 1500);

/* ------------------------------------------------------------------ explainer: how two unrelated tasks collide */
const EX = [
  { title: "Two agents get two tasks", text: "Agent A migrates orders-db. Agent B deploys payments. The tasks look unrelated.",
    nodes: { ordersdb: "tA", payments: "tB" }, edges: {}, agents: { A: "a", B: "b" } },
  { title: "The graph shows the link", text: "Checkout depends on orders, and orders depends on orders-db. Checkout also depends on payments. Both changes reach checkout, which customers use.",
    nodes: { ordersdb: "tA", orders: "zA", checkout: "both", payments: "tB" }, edges: { "orders-ordersdb": "a", "checkout-orders": "a", "checkout-payments": "b" }, agents: { A: "a", B: "b" }, note: true },
  { title: "Without Interlock", text: "Both changes run at the same time. Checkout fails. The logs show two changes, and nobody can say which one caused the failure.",
    nodes: { ordersdb: "run", payments: "run", orders: "zone", checkout: "fail" }, edges: { "orders-ordersdb": "red", "checkout-orders": "red", "checkout-payments": "red" }, agents: { A: "run", B: "run" } },
  { title: "With Interlock, agent B waits", text: "Agent B asks Interlock first. Interlock finds the overlap at checkout. Agent B waits for agent A.",
    nodes: { ordersdb: "run", orders: "zone", checkout: "zone", payments: "wait" }, edges: { "orders-ordersdb": "z", "checkout-orders": "z" }, agents: { A: "run", B: "wait" },
    labels: { B: "Agent B: waits for agent A" } },
  { title: "Agent A finishes, agent B runs", text: "Agent A releases its lock. Interlock wakes agent B. Both decisions are stored in the graph.",
    nodes: { payments: "run", checkout: "zone" }, edges: { "checkout-payments": "z" }, agents: { A: "done", B: "run" },
    labels: { A: "Agent A: done" } },
];
const EX_LABEL = { A: "Agent A: migrate orders-db", B: "Agent B: deploy payments" };
let exStep = 0, exTimer = null;

function exShow(i) {
  exStep = Math.max(0, Math.min(EX.length - 1, i));
  const s = EX[exStep], fig = $("#explainer");
  fig.querySelectorAll(".ex-node").forEach((g) => { g.setAttribute("class", `ex-node ${s.nodes[g.dataset.n] || ""}`); });
  fig.querySelectorAll(".ex-edges path").forEach((p) => { p.setAttribute("class", s.edges[p.dataset.e] || ""); });
  fig.querySelectorAll(".ex-agent").forEach((g) => {
    g.setAttribute("class", `ex-agent ${s.agents[g.dataset.a] || ""}`);
    const t = $("text", g);
    t.textContent = (s.labels && s.labels[g.dataset.a]) || EX_LABEL[g.dataset.a];
    $("rect", g).setAttribute("width", Math.ceil(t.getComputedTextLength()) + 36);
  });
  $(".ex-note", fig).classList.toggle("on", !!s.note);
  $("#exSteps").innerHTML = EX.map((x, k) => `<li class="${k === exStep ? "on" : ""} ${k < exStep ? "past" : ""}" data-k="${k}">
      <button type="button" aria-current="${k === exStep}"><span class="n">${k + 1}</span><b>${esc(x.title)}</b></button>
      ${k === exStep ? `<p>${esc(x.text)}</p>` : ""}</li>`).join("");
  document.querySelectorAll("#exSteps button").forEach((b) => b.addEventListener("click", () => { exStop(); exShow(Number(b.parentElement.dataset.k)); }));
  $("#exPrev").disabled = exStep === 0;
  $("#exNext").disabled = exStep === EX.length - 1;
}
function exStop() { clearInterval(exTimer); exTimer = null; $("#exPlay").textContent = "Play"; }
$("#exPrev").addEventListener("click", () => { exStop(); exShow(exStep - 1); });
$("#exNext").addEventListener("click", () => { exStop(); exShow(exStep + 1); });
$("#exPlay").addEventListener("click", () => {
  if (exTimer) return exStop();
  if (exStep === EX.length - 1) exShow(0);
  $("#exPlay").textContent = "Pause";
  exTimer = setInterval(() => { if (exStep === EX.length - 1) exStop(); else exShow(exStep + 1); }, 4200);
});
exShow(0);
document.fonts.ready.then(() => exShow(exStep));  // re-measure the agent tags in the real font

/* ------------------------------------------------------------------ scenarios that run on the live system */
const SC = [
  { id: "incident", title: "Outage", problem: "Checkout returns errors while other agents are busy.",
    what: "The incident agent asks to restart checkout. It has priority 0. Interlock pauses lower-priority changes that are in the way and grants the restart first.",
    agents: ["incident-responder"], kinds: ["incident", "bump"] },
  { id: "freeze", title: "Code freeze", problem: "The company freezes prod for a sale.",
    what: "Interlock blocks prod changes while the freeze is on. Only incident fixes with approval can pass.",
    kinds: ["freeze"], match: /freeze/i },
  { id: "rogue", title: "Rogue agents", problem: "A staging bot tries to delete a prod database. A sub-agent tries to deploy payments.",
    what: "Interlock blocks both. The bot is scoped to staging. No human in the sub-agent's chain can change payments.",
    agents: ["staging-cleaner", "payments-subagent"] },
  { id: "crash", title: "Agent crash", problem: "An agent crashes while it holds a lock.",
    what: "Each lock is a lease. The crashed agent sends no heartbeats, the lease expires after 40 seconds, and the waiting agents continue.",
    kinds: ["crash", "lapsed"] },
  { id: "deadlock", title: "Deadlock", problem: "Two agents each hold one resource and wait for the other's.",
    what: "Interlock finds the cycle in the WAITS_ON graph. It rolls back the less urgent agent. The other agent finishes.",
    agents: ["batch-bot-a", "batch-bot-b"], kinds: ["deadlock"], match: /batch-bot/ },
  { id: "migration", title: "Risky migration", problem: "An agent wants to re-shard payments-db.",
    what: "Payment databases need sign-off. The change waits for a human. Approve or reject it below as Arjun, the on-call engineer.",
    agents: ["migrator"], kinds: ["approval", "approved"], match: /payments-db/ },
];
let sc = SC[0], scSince = null, scWatch = null, scPending = null;

function renderScenarioList() {
  $("#scnList").innerHTML = SC.map((s) => `<button role="tab" type="button" data-id="${s.id}" aria-selected="${s === sc}"><b>${esc(s.title)}</b><span>${esc(s.problem)}</span></button>`).join("");
  document.querySelectorAll("#scnList button").forEach((b) => b.addEventListener("click", () => {
    sc = SC.find((s) => s.id === b.dataset.id);
    scSince = null; scPending = null; clearInterval(scWatch);
    $("#scnLog").innerHTML = "";
    renderScenarioList();
  }));
  $("#scnTitle").textContent = sc.title;
  $("#scnWhat").textContent = sc.what;
  renderScenarioActions();
}

function renderScenarioActions() {
  const box = $("#scnActions");
  if (!box) return;
  let html;
  if (sc.id === "freeze") {
    const on = STATE && STATE.freeze;
    html = `<button class="btn" type="button" data-run>${on ? "Lift the freeze" : "Turn the freeze on"}</button>${on ? '<span class="hint">The freeze is on now.</span>' : ""}`;
  } else {
    html = `<button class="btn" type="button" data-run>Run this scenario</button>`;
  }
  if (scPending) html += `<button class="btn ok" type="button" data-yes>Approve as Arjun</button><button class="btn ghost" type="button" data-no>Reject</button>`;
  if (box.dataset.html === html) return;  // keep focus on the button between refreshes
  box.dataset.html = html;
  box.innerHTML = html;
  $("[data-run]", box).addEventListener("click", runScenario);
  $("[data-yes]", box)?.addEventListener("click", () => decide(true));
  $("[data-no]", box)?.addEventListener("click", () => decide(false));
}

async function runScenario() {
  const before = await api("/api/state");
  scSince = before.seq;
  if (!before.running) await api("/api/sim", { action: "start" });
  const name = sc.id === "freeze" ? (before.freeze ? "freeze_off" : "freeze_on") : sc.id;
  const r = await api("/api/scenario", { name });
  scPending = r && r.status === "needs_approval" ? r.intent : null;
  $("#scnLog").innerHTML = r && r.error ? `<li><span class="t">Note</span><p>${esc(r.error)}. Try again in a few seconds.</p></li>` : "";
  readout();
  clearInterval(scWatch);
  const until = Date.now() + 25000;
  scWatch = setInterval(async () => { if (Date.now() > until) clearInterval(scWatch); await watchLog(); }, 900);
  watchLog();
}

async function watchLog() {
  if (scSince == null) return;
  const ev = await api(`/api/events?after=${scSince}`);
  const s = sc;
  const keep = ev.filter((e) => (s.agents || []).includes(e.agent) || (s.kinds || []).includes(e.kind) || (s.match && s.match.test(e.text)));
  if (!keep.length) return;
  const verdict = { granted: "Granted", waiting: "Waiting", denied: "Blocked", needs_approval: "Needs approval", approved: "Approved", rejected: "Rejected", aborted: "Rolled back", crashed: "Crashed", done: "Released" };
  $("#scnLog").innerHTML = keep.slice(-5).map((e) => `<li><span class="t">${esc(e.agent || "System")}</span>
     <p>${e.verdict && verdict[e.verdict] ? `<span class="badge ${esc(e.verdict)}">${esc(verdict[e.verdict])}</span> ` : ""}${esc(e.text)}</p></li>`).join("");
}

async function decide(yes) {
  if (!scPending) return;
  await api("/api/approve", { intent: scPending, human: "arjun", yes });
  scPending = null;
  renderScenarioActions();
  watchLog();
}
renderScenarioList();

/* ------------------------------------------------------------------ how it works */
document.querySelectorAll("#howSteps button").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll("#howSteps button").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
  document.querySelectorAll(".how-code pre").forEach((p) => { p.hidden = p.dataset.step !== b.dataset.step; });
}));
