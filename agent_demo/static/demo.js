"use strict";

const $ = id => document.getElementById(id);
const ACTIVE = new Set(["assigned", "running"]);
const ENDED = ["completed", "failed", "escalated", "stopped"];
const STATE_BADGE = {running: "good", assigned: "info", queued: "", waiting: "warn", needs_recovery: "bad",
                     completed: "good", superseded: "bad", failed: "bad", escalated: "bad", recovering: "warn", stopped: "warn"};
let state = null;
let open = null;          // {task, worker} shown in the drawer
let drawnRecords = -1;

// Build a DOM element. Text is always inserted as text, never as HTML.
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (key === "class") el.className = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (value !== false && value != null) el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child != null && child !== false) el.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return el;
}

async function api(path, body) {
  let response, data;
  try {
    response = await fetch(path, body ? {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)} : {cache: "no-store"});
    data = await response.json();
  } catch (error) {
    throw Object.assign(new Error("The orchestrator is not answering."), {offline: true});
  }
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

const badge = (text, kind) => h("span", {class: "badge " + (kind || "")}, text);
const clock = ts => new Date(ts * 1000).toLocaleTimeString([], {hour12: false});
const depth = id => id.split(".").length - 1;

// Display names: workloads are Alice, Bob, ...; agents "Alice Lead" / "Alice Sub 1"; VMs "VM 1", "VM 2".
let byId = {};
const taskName = id => (byId[id] && byId[id].name) || id;
const agentName = (taskId, workerId) => {
  const worker = byId[taskId] && byId[taskId].workers[workerId];
  return (worker && worker.label) || `${taskId}/${workerId}`;
};
const vmName = id => {
  const index = state ? state.vms.findIndex(vm => vm.id === id) : -1;
  return index < 0 ? (id || "no VM") : `VM ${index + 1}`;
};

function metric(label, value, note) {
  return h("div", {class: "metric"}, h("span", {}, label), h("strong", {}, value), h("small", {}, note || " "));
}

function renderMetrics() {
  const workers = state.tasks.flatMap(t => Object.values(t.workers));
  const live = workers.filter(w => ACTIVE.has(w.state)).length;
  const recoveries = workers.reduce((sum, w) => sum + w.failures, 0);
  const saved = state.saved;
  const span = s => s >= 90 ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s` : `${Math.round(s)}s`;
  $("metrics").replaceChildren(
    metric("Problems solved", `${state.score.solved} / ${state.score.total}`, `${state.tasks.length} task${state.tasks.length === 1 ? "" : "s"}`),
    metric("Agents running", live, `${workers.length} agents incl. subagents`),
    metric("Failures recovered", recoveries, "attempts replaced after a crash or a lost VM"),
    metric("Model calls saved", saved.calls, saved.calls
      ? `${span(saved.seconds)} of model time, ${saved.tokens.toLocaleString()} tokens not spent again`
      : "replies reused from the log after a failure"));
}

// ---------------------------------------------------------------- live system diagram
// Nodes are ordinary HTML; an SVG layer behind them draws one wire per VM to the
// model (LLM requests) and one to the orchestrator (assignments, heartbeats, log
// records). Counter changes between polls become dots travelling along the wires.
const SVG = "http://www.w3.org/2000/svg";
const FLOW = {request: "#8b4fd6", reply: "#c7a4f2", log: "#2f7fd1", assign: "#1f8760", beat: "#b3beb6"};
const calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const wires = {}, wireLabels = {}, particles = [];
let previous = null;      // counters from the last poll

function svg(tag, attrs) {
  const el = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs || {})) el.setAttribute(key, value);
  return el;
}

function wire(key, kind) {
  if (!wires[key]) {
    wires[key] = svg("path", {class: "wire " + kind, fill: "none"});
    wireLabels[key] = svg("text", {class: "wire-label " + kind, "text-anchor": "middle"});
    $("wires").prepend(wires[key]);
    $("wires").append(wireLabels[key]);
  }
  return wires[key];
}

// Position every wire from the current on-screen boxes of the nodes it joins.
function layoutWires() {
  const frame = $("topology").getBoundingClientRect();
  if (!frame.width) return;
  const box = el => { const r = el.getBoundingClientRect(); return {l: r.left - frame.left, t: r.top - frame.top, w: r.width, h: r.height}; };
  $("wires").setAttribute("viewBox", `0 0 ${frame.width} ${frame.height}`);
  const model = box($("node-model")), orch = box($("node-orch")), store = box($("node-log"));
  const curve = (key, kind, x1, y1, x2, y2) => {
    const bend = (y1 - y2) * 0.55;
    wire(key, kind).setAttribute("d", `M ${x1} ${y1} C ${x1} ${y1 - bend}, ${x2} ${y2 + bend}, ${x2} ${y2}`);
  };
  state.vms.forEach((vm, index) => {
    const el = document.querySelector(`[data-vm="${CSS.escape(vm.id)}"]`);
    if (!el) return;
    const v = box(el), share = (index + 1) / (state.vms.length + 1);
    curve("llm:" + vm.id, "llm", v.l + v.w * 0.3, v.t, model.l + model.w * share, model.t + model.h);
    curve("ctl:" + vm.id, "ctl", v.l + v.w * 0.7, v.t, orch.l + orch.w * share, orch.t + orch.h);
    const llm = state.llm.by_vm[vm.id] || {total: 0, in_flight: 0};
    wires["llm:" + vm.id].classList.toggle("active", llm.in_flight > 0);
    for (const key of ["llm:" + vm.id, "ctl:" + vm.id]) wires[key].classList.toggle("down", !vm.online || vm.silent);
    label("llm:" + vm.id, 0.14, `${llm.total} model calls`);
    label("ctl:" + vm.id, 0.14, `${state.log.by_vm[vm.id] || 0} log writes`);
  });
  wire("store", "ctl").setAttribute("d", `M ${orch.l + orch.w} ${orch.t + orch.h / 2} L ${store.l} ${store.t + store.h / 2}`);

  // One line from each placed agent up to the VM slot it occupies.
  const thinking = new Set(state.llm.open.map(c => `${c.task_id}/${c.worker_id}`));
  const placed = new Set();
  for (const tile of document.querySelectorAll("[data-agent]")) {
    const slot = document.querySelector(`[data-slot="${CSS.escape(tile.dataset.agent)}"]`);
    if (!slot) continue;
    const key = "place:" + tile.dataset.agent, a = box(tile), b = box(slot);
    placed.add(key);
    curve(key, "place", a.l + a.w / 2, a.t, b.l + b.w / 2, b.t + b.h);
    wires[key].classList.toggle("active", thinking.has(tile.dataset.agent));
  }
  for (const key of Object.keys(wires)) {
    if (key.startsWith("place:") && !placed.has(key)) {
      wires[key].remove(); wireLabels[key].remove();
      delete wires[key]; delete wireLabels[key];
    }
  }
}

// Where one agent stands on each of its problems, counting its subagents' work.
function progress(task, worker) {
  const family = Object.values(task.workers).filter(w => w.id === worker.id || w.id.startsWith(worker.id + "."));
  const cells = worker.node.problems.map(id => {
    const holders = family.filter(w => w.node.problems.includes(id));
    const grades = holders.map(w => w.result && w.result.problems && w.result.problems[id]).filter(Boolean);
    const reviews = holders.map(w => !w.result && w.review && w.review[id]).filter(Boolean);
    const checks = holders.map(w => w.checks[id]).filter(Boolean);
    if ([...grades, ...reviews].some(g => g.total && g.passed === g.total)) return "solved";
    if (grades.length === holders.length) return "failed";
    if (reviews.length) return "failing";
    if (checks.some(c => c.passed === c.total)) return "passing";
    if (checks.length) return "failing";
    return holders.length > 1 ? "delegated" : "todo";
  });
  const graded = !!worker.result && family.every(w => w.result);
  const done = cells.filter(c => c === "solved" || (!graded && c === "passing")).length;  // provisional until graded
  return {cells, done, total: cells.length, graded};
}

// Seconds until the orchestrator will notice that this agent's process is gone
// (killed directly, or lost with its VM); 0 if the process is not known to be dead.
function deadFor(worker) {
  if (!ACTIVE.has(worker.state)) return 0;
  if (worker.killed) return Math.max(1, worker.lease_left);
  const vm = state.vms.find(v => v.id === worker.vm_id);
  return vm && vm.silent ? Math.max(1, vm.lease_left) : 0;
}

const PLACE = {assigned: "starting on", running: "on", queued: "waiting for a free slot", waiting: "paused until its subagents finish",
               needs_recovery: "lost; waiting for recovery", completed: "finished", superseded: "stopped"};

function agentTile(task, worker, thinking) {
  const key = `${task.id}/${worker.id}`, live = ACTIVE.has(worker.state), p = progress(task, worker);
  const lead = !worker.parent_id, dead = deadFor(worker), nested = depth(worker.id) > 1;
  return h("div", {class: `tile ${worker.state}${thinking.has(key) && !dead ? " thinking" : ""}${lead ? "" : " sub"}${dead ? " dead" : ""}`, "data-agent": key},
    h("div", {class: "tile-head"}, h("strong", {}, worker.label),
      h("span", {class: "tile-score"}, `${p.done}/${p.total}`)),
    nested && h("span", {class: "tile-parent"}, `↳ started by ${task.workers[worker.parent_id].label}`),
    h("div", {class: "bar", title: "one segment per problem"}, p.cells.map(c => h("i", {class: c}))),
    h("small", {}, dead ? `process dead · orchestrator notices in ${dead}s`
                 : worker.held ? `held back: ${state.reserve.slots} slots are kept free for recovery`
                 : live ? `${PLACE[worker.state]} ${vmName(worker.vm_id)} · turn ${worker.log.turns || 0}/${worker.node.step_limit}`
                        : PLACE[worker.state] || worker.state, worker.failures ? ` · recovered ×${worker.failures}` : "",
      worker.reviews ? ` · sent back ×${worker.reviews}` : ""),
    h("div", {class: "tile-actions"},
      h("button", {class: "ghost", type: "button", onclick: () => openDrawer(task.id, worker.id)}, "Trajectory"),
      live && !dead && h("button", {class: "danger", type: "button", title: "Kill this agent's process without telling the orchestrator",
                           onclick: () => startScenario({kind: "agent", task_id: task.id, worker_id: worker.id})}, "Crash agent")));
}

function renderAgentLayer() {
  const thinking = new Set(state.llm.open.map(c => `${c.task_id}/${c.worker_id}`));
  const groups = state.tasks.map(task => {
    const workers = Object.values(task.workers).sort((a, b) => a.id.localeCompare(b.id, undefined, {numeric: true}));
    const overall = progress(task, task.workers.agent);
    const finished = ENDED.includes(task.status);
    const head = h("div", {class: "group-head", title: task.id}, h("strong", {}, task.name), badge(task.status, STATE_BADGE[task.status]),
      h("button", {class: "danger", type: "button", title: "Stop this run's agents and remove it from the board",
                   onclick: () => act("/clear_runs", {task_id: task.id})}, finished ? "Clear" : "Stop & clear"),
      h("span", {class: "group-score"}, `${overall.done}/${overall.total} ${finished ? "solved" : "done"}`));
    if (finished) return h("div", {class: "group done"}, head, h("div", {class: "bar"}, overall.cells.map(c => h("i", {class: c}))));
    return h("div", {class: "group"}, head, h("div", {class: "tiles"}, workers.map(w => agentTile(task, w, thinking))));
  });
  $("agent-layer").replaceChildren(...(groups.length ? groups : [h("p", {class: "empty"}, "No agents yet. Launch some below.")]));
}

function label(key, at, text) {
  const path = wires[key], point = path.getPointAtLength(path.getTotalLength() * at);
  wireLabels[key].setAttribute("x", point.x);
  wireLabels[key].setAttribute("y", point.y);
  wireLabels[key].textContent = text;
}

// Send one dot along a wire. Wires run from the VM (start) up to the laptop (end).
function emit(key, kind, options = {}) {
  if (calm || !wires[key] || particles.length > 160) return;
  const dot = svg("circle", {r: options.r || 4.5, fill: FLOW[kind], class: "dot", opacity: 0});
  $("wires").append(dot);
  particles.push({key, dot, down: !!options.down, start: performance.now() + (options.delay || 0),
                  duration: options.duration || 850, then: options.then});
}

function animate(now) {
  for (let i = particles.length - 1; i >= 0; i--) {
    const p = particles[i], t = (now - p.start) / p.duration;
    if (t < 0) continue;
    const path = wires[p.key];
    if (t >= 1 || !path) {
      p.dot.remove();
      particles.splice(i, 1);
      if (p.then) p.then();
      continue;
    }
    const eased = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
    const point = path.getPointAtLength(path.getTotalLength() * (p.down ? 1 - eased : eased));
    p.dot.setAttribute("cx", point.x);
    p.dot.setAttribute("cy", point.y);
    p.dot.setAttribute("opacity", 1);
  }
  requestAnimationFrame(animate);
}

function pulse(el) {
  if (!el) return;
  el.classList.remove("pulse");
  void el.offsetWidth;
  el.classList.add("pulse");
}

// Turn what changed since the last poll into traffic on the wires.
function flows() {
  const current = {seq: Math.max(0, ...state.events.map(e => e.seq)), vms: {}};
  for (const vm of state.vms) {
    const llm = state.llm.by_vm[vm.id] || {total: 0, done: 0};
    current.vms[vm.id] = {requests: llm.total, replies: llm.done, logs: state.log.by_vm[vm.id] || 0};
  }
  if (previous) {
    const spread = () => Math.random() * 800;
    for (const vm of state.vms) {
      const now = current.vms[vm.id], before = previous.vms[vm.id] || now;
      const node = document.querySelector(`[data-vm="${CSS.escape(vm.id)}"]`);
      for (let i = 0; i < Math.min(6, now.requests - before.requests); i++)
        emit("llm:" + vm.id, "request", {delay: spread(), then: () => pulse($("node-model"))});
      for (let i = 0; i < Math.min(6, now.replies - before.replies); i++)
        emit("llm:" + vm.id, "reply", {down: true, delay: spread(), then: () => pulse(node)});
      for (let i = 0; i < Math.min(6, now.logs - before.logs); i++)
        emit("ctl:" + vm.id, "log", {delay: spread(), then: () => emit("store", "log", {duration: 450, then: () => pulse($("node-log"))})});
      if (vm.online && !vm.silent) emit("ctl:" + vm.id, "beat", {r: 2.5, delay: spread(), duration: 1100});
    }
    for (const event of state.events) {
      if (event.seq > previous.seq && event.type === "worker_assigned")
        emit("ctl:" + event.payload.vm_id, "assign", {down: true, r: 5.5, delay: spread(),
             then: () => pulse(document.querySelector(`[data-vm="${CSS.escape(event.payload.vm_id)}"]`))});
    }
  }
  previous = current;
}

function renderTopology() {
  const workers = state.tasks.flatMap(t => Object.values(t.workers).map(w => ({...w, task: t.id, live: !ENDED.includes(t.status)})));
  const count = wanted => workers.filter(w => w.live && wanted.includes(w.state)).length;
  const llm = state.llm;
  const thinking = new Set(llm.open.map(c => `${c.task_id}/${c.worker_id}`));

  $("node-model").replaceChildren(
    h("p", {class: "eyebrow"}, "MODEL · OLLAMA"), h("h3", {}, state.defaults.model),
    h("p", {class: "node-stat"}, h("strong", {}, llm.in_flight), ` in flight · ${llm.total} total${llm.avg_latency ? ` · ${llm.avg_latency.toFixed(1)}s avg` : ""}`),
    h("ul", {class: "calls"}, llm.recent.slice(0, 5).map(c => h("li", {class: c.ended ? (c.ok ? "" : "bad") : "open"},
      h("span", {}, `${vmName(c.vm_id)} · ${agentName(c.task_id, c.worker_id)}`),
      h("span", {}, c.ended ? (c.ok ? `${c.latency.toFixed(1)}s · ${c.completion_tokens ?? "?"} tok` : "aborted") : "generating…")))));
  $("node-orch").replaceChildren(
    h("p", {class: "eyebrow"}, "ORCHESTRATOR"), h("h3", {}, "Scheduling · leases · recovery"),
    h("p", {class: "node-stat"}, h("strong", {}, count(["assigned", "running"])), " agents placed on VMs"),
    h("p", {class: "node-stat"}, h("strong", {}, count(["waiting"])), " paused for subagents (no slot held)"),
    h("p", {class: "node-stat"}, h("strong", {}, count(["queued", "needs_recovery"])), " waiting for a slot or recovery"),
    state.reserve.slots ? h("p", {class: "node-stat"}, h("strong", {}, `${state.reserve.free}`),
      ` slots free · ${state.reserve.slots} kept free to restart failed agents`) : null);
  $("node-log").replaceChildren(
    h("p", {class: "eyebrow"}, "DURABLE LOG · SQLITE"), h("h3", {}, "What recovery replays"),
    h("p", {class: "node-stat"}, h("strong", {}, state.log.log_records), " agent turns and file snapshots"),
    h("p", {class: "node-stat"}, h("strong", {}, state.log.events), " orchestrator events"),
    h("p", {class: "node-stat"}, h("strong", {}, state.log.spawns), " spawn receipts (exactly-once)"));

  $("vms").replaceChildren(...state.vms.map(vm => {
    const slots = [];
    for (let i = 0; i < vm.capacity; i++) {
      const a = vm.assignments[i];
      const gone = a && (vm.silent || (byId[a.task_id] && byId[a.task_id].workers[a.id].killed));
      const busy = a && !gone && thinking.has(`${a.task_id}/${a.id}`);
      slots.push(a
        ? h("div", {class: "slot" + (busy ? " thinking" : "") + (gone ? " dead" : ""), "data-slot": `${a.task_id}/${a.id}`}, h("strong", {}, agentName(a.task_id, a.id)),
            h("small", {}, `attempt ${a.attempt} · pid ${a.pid ?? "…"}`),
            h("small", {class: "doing"}, gone ? "process dead · not noticed yet" : busy ? "waiting on the model" : "running a command"))
        : h("div", {class: "slot empty"}, "free slot"));
    }
    return h("div", {class: "vm" + (vm.silent ? " silent" : vm.online ? "" : " offline"), "data-vm": vm.id},
      h("div", {class: "vm-head"}, h("div", {}, h("h3", {title: vm.id}, vmName(vm.id)),
          badge(vm.silent ? "not responding" : vm.online ? "online" : "offline", vm.silent ? "warn" : vm.online ? "good" : "bad"),
          h("span", {class: "vm-note"},
            vm.silent ? `no heartbeat for ${vm.silent_for}s · lease runs out in ${vm.lease_left}s` :
            vm.online ? `${vm.assignments.length}/${vm.capacity} slots` :
            vm.down_for ? `declared lost · restarts in ${vm.down_for}s` : "no heartbeat")),
        h("div", {class: "vm-actions"},
          vm.online && !vm.silent && h("button", {class: "danger", type: "button",
            title: "Power loss, without telling the orchestrator: its agents die, and it rejoins after its downtime",
            onclick: () => startScenario({kind: "vm", vm_id: vm.id})}, "Crash VM"))),
      h("div", {class: "slots"}, slots));
  }));
  renderAgentLayer();
  applyFocus();
  layoutWires();
  flows();
}

function renderFlowLegend() {
  const item = (kind, text) => h("span", {}, h("i", {style: `background:${FLOW[kind]}`}), text);
  $("flow-legend").replaceChildren(item("request", "prompt to model"), item("reply", "model reply"),
    item("log", "durable log write"), item("assign", "assignment"), item("beat", "heartbeat"),
    h("span", {}, h("i", {class: "seg passing"}), "passes examples"), h("span", {}, h("i", {class: "seg solved"}), "passed hidden tests"),
    h("span", {}, h("i", {class: "seg failing"}), "failing"), h("span", {}, h("i", {class: "seg delegated"}), "with a subagent"));
}

window.addEventListener("resize", () => state && layoutWires());
requestAnimationFrame(animate);

// A problem chip as seen by one agent: its own result, a live check, or delegated.
function chip(task, worker, id) {
  const info = task.problems[id];
  const grade = worker.result && worker.result.problems && worker.result.problems[id];
  const check = worker.checks[id];
  const delegated = Object.values(task.workers).some(w => w.parent_id === worker.id && w.node.problems.includes(id));
  let kind = "", text = "";
  if (grade) {
    const solved = grade.total && grade.passed === grade.total;
    kind = solved ? "solved" : (delegated && info.status === "solved" ? "delegated" : "failed");
    text = kind === "delegated" ? "→ subagent" : `${grade.passed}/${grade.total}`;
  } else if (worker.review && worker.review[id] && !delegated) {
    const r = worker.review[id];
    kind = r.passed === r.total ? "solved" : "partial";
    text = `hidden ${r.passed}/${r.total}`;
  } else if (check) {
    kind = check.passed === check.total ? "checking" : "partial";
    text = `examples ${check.passed}/${check.total}`;
  } else if (delegated) {
    kind = "delegated"; text = "→ subagent";
  }
  return h("span", {class: "chip " + kind, title: `${info.title} (${info.difficulty})`},
    h("b", {}, info.difficulty[0].toUpperCase()), id, text && h("span", {}, text));
}

function workerRow(task, worker) {
  const d = depth(worker.id);
  const meta = [badge(worker.state.replace("_", " "), STATE_BADGE[worker.state])];
  if (worker.vm_id && ACTIVE.has(worker.state)) meta.push(h("span", {}, vmName(worker.vm_id)));
  meta.push(h("span", {}, `${worker.log.turns || 0}/${worker.node.step_limit} turns`));
  if (worker.failures) meta.push(badge(`recovered ×${worker.failures}`, "bad"));
  if (worker.reviews) meta.push(badge(`sent back ×${worker.reviews}`, "warn"));
  if (worker.result) meta.push(h("span", {}, worker.result.exit_status));
  return h("div", {class: "worker"},
    h("div", {}, h("div", {class: "worker-name"}, d ? h("span", {class: "tree"}, "  ".repeat(d - 1) + "└─") : null,
      worker.label), h("div", {class: "worker-meta"}, meta)),
    h("div", {class: "chips"}, worker.node.problems.map(id => chip(task, worker, id))),
    h("div", {class: "worker-actions"},
      h("button", {class: "ghost", type: "button", onclick: () => openDrawer(task.id, worker.id)}, "Trajectory"),
      worker.parent_id && h("button", {class: "ghost", type: "button", title: "Walk through how this subagent was started",
        onclick: () => startScenario({kind: "spawn", task_id: task.id, worker_id: worker.id})}, "Lifecycle"),
      ACTIVE.has(worker.state) && !deadFor(worker) && h("button", {class: "danger", type: "button",
        onclick: () => startScenario({kind: "agent", task_id: task.id, worker_id: worker.id})}, "Crash agent")));
}

function renderTasks() {
  const tasks = [...state.tasks].reverse();
  $("agent-count").textContent = `${tasks.length} task${tasks.length === 1 ? "" : "s"}`;
  if (!tasks.length) return $("tasks").replaceChildren(h("p", {class: "empty"}, "No agents yet. Launch some on the right."));
  $("tasks").replaceChildren(...tasks.map(task => {
    const problems = Object.values(task.problems);
    const solved = problems.filter(p => p.status === "solved").length;
    const order = Object.values(task.workers).sort((a, b) => a.id.localeCompare(b.id, undefined, {numeric: true}));
    return h("div", {class: "task"},
      h("div", {class: "task-head"}, h("h3", {}, task.name, badge(task.status, STATE_BADGE[task.status]),
        h("span", {class: "hint"}, `${task.id} · ${task.spec.model}`),
        h("button", {class: "danger", type: "button", onclick: () => act("/clear_runs", {task_id: task.id})},
          ENDED.includes(task.status) ? "Clear" : "Stop & clear")), h("span", {class: "task-score"}, `${solved} / ${problems.length}`)),
      task.reason && h("p", {class: "task-reason"}, task.reason),
      order.map(worker => workerRow(task, worker)));
  }));
}

const who = e => agentName(e.task_id, e.worker_id);
const EVENTS = {
  worker_assigned: e => ["info", `${who(e)} placed on ${vmName(e.payload.vm_id)}`, `attempt ${e.attempt}`],
  process_lost: e => ["bad", `Injected failure: ${who(e)}'s process on ${vmName(e.payload.vm_id)} died`,
    `${e.payload.cause} · the orchestrator was not told`],
  spawn_replayed: e => ["good", `${who(e)} ran a spawn command again and got ${agentName(e.task_id, e.payload.child_id)} back`,
    "same operation id · no second subagent was created"],
  orchestrator_restarted: e => ["info", `The orchestrator restarted after ${e.payload.down}s and continued from the durable log`,
    `${e.payload.leases.vms} VM and ${e.payload.leases.agents} agent leases given a fresh window · no agent restarted`],
  attempt_superseded: e => ["bad", `Orchestrator detected that ${who(e)} was lost`, `${e.payload.reason} · attempt ${e.attempt} fenced`],
  recovery_completed: e => ["good", `${who(e)} has a new process`, `attempt ${e.attempt}`],
  log_replayed: e => [e.payload.after === "failure" ? "good" : "", `${who(e)} rebuilt from its durable log`,
    `${e.payload.records} records · ${e.payload.turns} model replies · ${e.payload.files} files · no model call repeated`
    + (e.payload.after === "failure" && e.payload.saved_seconds ? ` · ${Math.round(e.payload.saved_seconds)}s of model time saved` : "")],
  subtask_spawned: e => ["info", `${agentName(e.task_id, e.payload.by)} started ${who(e)}`, e.payload.problems.join(", ")],
  subtask_recovery_authorized: e => ["info", `${agentName(e.task_id, e.payload.by)} authorized recovery of ${who(e)}`, ""],
  agent_suspended: e => ["", `${who(e)} released its slot to wait for subagents`, ""],
  worker_completed: e => ["good", `${who(e)} finished`, Object.entries(e.payload.summary || {}).map(([p, s]) => `${p} ${s}`).join(", ")],
  workload_completed: e => ["good", `${taskName(e.task_id)} complete: ${e.payload.result.solved}/${e.payload.result.total} solved`, ""],
  review_rejected: e => ["bad", `${who(e)} said it was done, but the orchestrator's check failed: sent back to work`,
    `still failing: ${e.payload.failing.join(", ")} · check ${e.payload.check} of ${e.payload.of}`],
  step_completed: e => [e.payload.accepted === false ? "" : "good",
    `${who(e)}: orchestrator check ${e.payload.accepted === false ? "still failing, no checks left" : "passed"}`,
    Object.entries(e.payload.summary || {}).map(([p, v]) => `${p} ${v}`).join(", ")],
  workload_stopped: e => ["bad", `${taskName(e.task_id)} stopped by operator`, ""],
  workload_escalated: e => ["bad", `${taskName(e.task_id)} escalated`, e.payload.reason],
  workload_failed: e => ["bad", `${taskName(e.task_id)} failed`, e.payload.reason],
};

function renderEvents() {
  const rows = state.events.filter(e => EVENTS[e.type]).map(e => {
    const [kind, text, detail] = EVENTS[e.type](e);
    return h("div", {class: "event " + kind}, h("time", {}, clock(e.timestamp)), text, detail && h("small", {}, detail));
  });
  $("events").replaceChildren(...(rows.length ? rows : [h("p", {class: "empty"}, "Nothing has happened yet.")]));
}

function renderLegend() {
  $("legend").replaceChildren(
    h("span", {class: "chip"}, "not started"), h("span", {class: "chip checking"}, "passes examples"),
    h("span", {class: "chip partial"}, "fails examples"), h("span", {class: "chip delegated"}, "→ handed to subagent"),
    h("span", {class: "chip solved"}, "passed hidden tests"), h("span", {class: "chip failed"}, "failed hidden tests"),
    h("span", {}, "E / M / H = easy, medium, hard"));
}

async function act(path, body) {
  try { await api(path, body); await refresh(); } catch (error) { showError(error); }
}

function showError(error) {
  $("error").textContent = error ? `Request failed: ${error.message}` : "";
  $("error").hidden = !error;
}

// ---------------------------------------------------------------- trajectory drawer
// seek: open at the point where the agent was last rebuilt after a failure, not at the end.
function openDrawer(task, worker, seek) {
  open = {task, worker, seek: !!seek};
  drawnRecords = -1;
  $("drawer-title").textContent = agentName(task, worker);
  $("drawer-body").replaceChildren(h("p", {class: "empty"}, "Loading…"));
  $("drawer").hidden = false;
  refreshDrawer();
}

function splitReply(content) {
  const match = /```mswea_bash_command\s*\n([\s\S]*?)\n```/.exec(content || "");
  return match ? [content.slice(0, match.index).trim(), match[1]] : [(content || "").trim(), null];
}

function recordView(record) {
  const data = record.data, messages = data.messages || [];
  const body = [];
  if (record.kind === "init") {
    body.push(h("details", {}, h("summary", {}, "Task prompt given to the agent"), h("pre", {}, messages.map(m => m.content).join("\n\n"))));
  } else if (record.kind === "query") {
    const [thought, command] = splitReply(messages[0] && messages[0].content);
    if (thought) body.push(h("p", {}, thought));
    if (command) body.push(h("pre", {class: "cmd"}, command));
  } else if (record.kind === "grade") {
    body.push(h("p", {}, `Graded by the orchestrator on the hidden tests (${data.exit_status}).`));
    body.push(h("table", {class: "grades"}, Object.entries(data.grades).map(([id, g]) => {
      const failing = (g.cases || []).find(c => !c.ok);
      return h("tr", {}, h("td", {}, id), h("td", {}, badge(`${g.passed}/${g.total}`, g.total && g.passed === g.total ? "good" : "bad")),
        h("td", {}, g.error || (failing ? `first failure: args ${JSON.stringify(failing.args)} → ${failing.got ?? failing.error}, expected ${JSON.stringify(failing.expected)}` : "")));
    })));
  } else {
    for (const m of messages) {
      const extra = m.extra || {};
      if (record.kind === "observation" && extra.raw_output != null) {
        body.push(h("div", {class: "files"}, `exit code ${extra.returncode}`));
        body.push(h("pre", {}, extra.raw_output || "(no output)"));
      } else {
        body.push(h("pre", {}, m.content));
      }
    }
  }
  if (data.checks) body.push(h("div", {class: "files"}, "example tests: " + Object.entries(data.checks).map(([p, c]) => `${p} ${c.passed}/${c.total}`).join(", ")));
  if (data.files) body.push(h("div", {class: "files"}, "files written: " + Object.keys(data.files).join(", ")));
  if (data.suspend) body.push(h("div", {class: "files"}, "slot released until subagents finish"));
  const label = {init: "start", query: "model reply", observation: "command output", format_error: "format error",
                 children: "subagent results", grade: "orchestrator grade", exit: "exit",
                 review: "orchestrator check: sent back to work"}[record.kind] || record.kind;
  return h("div", {class: "rec " + record.kind},
    h("div", {class: "rec-head"}, h("span", {}, `#${record.seq} · ${label}`), h("span", {}, `${vmName(record.vm_id)} · attempt ${record.attempt} · ${clock(record.ts)}`)), body);
}

async function refreshDrawer() {
  if (!open) return;
  let view;
  try { view = await api(`/demo/trajectory?task_id=${encodeURIComponent(open.task)}&worker_id=${encodeURIComponent(open.worker)}`); } catch (error) { return; }
  const worker = view.worker;
  $("drawer-sub").textContent = `${worker.state} · attempt ${worker.attempt}${worker.vm_id ? " on " + vmName(worker.vm_id) : ""} · problems: ${worker.node.problems.join(", ")}`;
  if (view.records.length === drawnRecords) return;
  drawnRecords = view.records.length;
  const body = $("drawer-body");
  const pinned = body.scrollHeight - body.scrollTop - body.clientHeight < 80;
  const nodes = [];
  view.records.forEach((record, index) => {
    const previous = view.records[index - 1];
    if (previous && previous.attempt !== record.attempt) {
      const lost = view.lost_attempts.includes(previous.attempt);
      nodes.push(h("div", {class: "divider" + (lost ? "" : " calm")},
        lost ? `Recovered: attempt ${previous.attempt} on ${vmName(previous.vm_id)} was lost, attempt ${record.attempt} continues on ${vmName(record.vm_id)}`
             : `Resumed on ${vmName(record.vm_id)} (attempt ${record.attempt}) after waiting for subagents`,
        h("small", {}, `${index} records restored from the durable log. No logged model reply was requested again.`)));
    }
    nodes.push(recordView(record));
  });
  body.replaceChildren(...(nodes.length ? nodes : [h("p", {class: "empty"}, "This agent has not started yet.")]));
  const rebuilt = open.seek && [...body.querySelectorAll(".divider:not(.calm)")].pop();
  open.seek = false;
  if (rebuilt) rebuilt.scrollIntoView({block: "center"});
  else if (pinned) body.scrollTop = body.scrollHeight;
}

$("drawer-close").addEventListener("click", () => { open = null; $("drawer").hidden = true; });

// ---------------------------------------------------------------- scenarios and their walkthroughs
// A scenario asks the orchestrator to inject a failure (or pick a spawn to follow)
// and then polls /demo/drill, which reports each step from durable events. Scenarios
// can be started in any order and several can be in progress; the panel shows one
// and lists the others.
const SCENARIOS = {agent: "Crash an agent", lead: "Crash a lead agent", sub: "Crash a subagent", vm: "Crash a VM",
                   spawn_crash: "Crash an agent mid-spawn", spawn: "Follow a subagent spawn"};
const CARDS = [
  {kind: "agent", bad: true, does: () => "Kills one running agent's process. Its VM stays healthy.",
   shows: "detection by lease, rebuild from the log, no model call repeated"},
  {kind: "vm", bad: true, does: t => `Power loss: every agent on the VM dies. It restarts after ${t.crash}s.`,
   shows: "many recoveries at once, using the slots kept free for recovery"},
  {kind: "spawn_crash", bad: true, does: () => "Kills the next agent that starts a subagent, right after the receipt is stored.",
   shows: "exactly-once: the re-run command gets the same subagent back"},
  {kind: "spawn", does: () => "Injects nothing. Follows the most recent spawn.",
   shows: "how a subagent comes to exist and reports back"},
];
const cardEls = {};
let drills = [];          // walkthroughs of this session, most recent first
let drill = null;         // the one in the panel: {want, desc, view, message, offline, shown, told}
let down = null;          // when the orchestrator stopped answering (ms), else null
const downFor = () => Math.max(1, Math.round((Date.now() - down) / 1000));

// The cards are built once and updated in place, so a click is never lost to a re-render.
function renderCards() {
  const timing = state.defaults.timing;
  for (const card of CARDS) {
    if (!cardEls[card.kind]) {
      const el = h("button", {type: "button", class: "scenario" + (card.bad ? " bad" : ""), onclick: () => startScenario({kind: card.kind})},
        h("strong", {}, SCENARIOS[card.kind]), h("span", {class: "does"}), h("span", {class: "shows"}, h("b", {}, "Shows"), " " + card.shows),
        h("span", {class: "target"}));
      cardEls[card.kind] = el;
      $("scenario-cards").append(el);
    }
    const el = cardEls[card.kind], live = state.scenarios[card.kind] || {available: false, reason: ""};
    el.disabled = !live.available;
    el.classList.toggle("armed", !!live.armed);
    el.querySelector(".does").textContent = card.does(timing);
    const target = el.querySelector(".target");
    target.textContent = live.available ? `Target: ${live.target}` : `Not now: ${live.reason}`;
    target.classList.toggle("none", !live.available);
  }
  $("drill-reopen").hidden = !!drill || !drills.length;
}

function startScenario(want) {
  drill = {want, desc: null, view: null, message: "Starting…", offline: false, shown: null, told: false};
  drills = [drill, ...drills].slice(0, 5);
  showDrill();
  // Bring the diagram next to the panel, so what the steps describe can be watched.
  $("live").scrollIntoView({block: "start", behavior: calm ? "auto" : "smooth"});
  refreshDrill().then(refresh);
}

function showDrill() {
  $("drill").hidden = false;
  document.body.classList.add("drill-open");
  renderDrill();
  if (state) layoutWires();
}

function closeDrill() {
  if (drill && !drill.desc) drills = drills.filter(d => d !== drill);   // it never started: closing cancels it
  drill = null;
  $("drill").hidden = true;
  document.body.classList.remove("drill-open");
  applyFocus();
  if (state) { renderCards(); layoutWires(); }
}

async function refreshDrill() {
  const mine = drill;
  if (!mine) return;
  if (!mine.desc) {
    try { mine.desc = await api("/scenario", mine.want); mine.message = null; }
    catch (error) { mine.message = error.message; mine.offline = !!error.offline; }
  }
  if (mine.desc) {
    try { mine.view = await api("/demo/drill?" + new URLSearchParams(mine.desc)); mine.offline = false; mine.message = null; }
    catch (error) {
      mine.offline = !!error.offline;     // an unreachable orchestrator keeps the last view on screen
      if (!error.offline) { mine.view = null; mine.message = /armed/.test(error.message) ? error.message : "This run is no longer on the board."; }
    }
  }
  if (drill === mine) renderDrill();
}

// Outline what the walkthrough in the panel is about, so the panel and the diagram connect.
function applyFocus() {
  for (const el of document.querySelectorAll(".focus")) el.classList.remove("focus");
  const focus = drill && drill.view && drill.view.focus;
  if (!focus) return;
  const mark = selector => { const el = document.querySelector(selector); if (el) el.classList.add("focus"); };
  for (const id of focus.vms || []) mark(`[data-vm="${CSS.escape(id)}"]`);
  for (const key of focus.agents || []) { mark(`[data-agent="${CSS.escape(key)}"]`); mark(`[data-slot="${CSS.escape(key)}"]`); }
  if (focus.orchestrator) $("node-orch").classList.add("focus");
}

const stamp = at => at == null ? null : h("time", {}, at ? `+${at.toFixed(1)}s` : "start");

function stepView(step, index) {
  const wait = step.wait;
  return h("li", {class: "step " + step.state, "data-step": step.key},
    h("span", {class: "step-mark"}, step.state === "done" ? "✓" : step.state === "skipped" ? "–" : index + 1),
    h("div", {class: "step-main"},
      h("div", {class: "step-head"}, h("h3", {}, step.title), step.state === "active" ? badge("now", "warn") : stamp(step.at)),
      h("p", {}, step.text),
      step.code && h("pre", {class: "cmd"}, step.code),
      wait && h("div", {class: "wait"}, h("i", {style: `width:${Math.min(100, 100 * wait.elapsed / wait.total)}%`}),
        h("span", {}, `${Math.min(wait.total, wait.elapsed).toFixed(0)}s of the ${wait.total.toFixed(0)}s ${wait.label} used`)),
      step.items.length ? h("ul", {class: "step-items"}, step.items.map(item => h("li", {class: item.state},
        h("strong", {}, item.label),
        h("span", {}, item.text || (item.state === "done" ? "" : "not yet"),
          item.open && h("button", {class: "link", type: "button", title: "Open this agent's trajectory at the point where it was rebuilt",
                                    onclick: () => openDrawer(item.open.task, item.open.worker, true)}, "see trajectory")),
        stamp(item.at)))) : null));
}

function renderDrill() {
  if (!drill) return;
  const view = drill.view, card = CARDS.find(c => c.kind === drill.want.kind);
  $("drill-title").textContent = view ? view.title : SCENARIOS[drill.want.kind];
  $("drill-sub").textContent = !view ? "" : view.ended ? `${view.subtitle} · the run was ${view.ended} before this finished`
    : view.waiting ? view.subtitle
    : `${view.subtitle} · ${view.done ? "all steps complete" : `${view.elapsed.toFixed(0)}s since it began`}`;
  const shows = view ? view.shows : card && card.shows;
  $("drill-shows").hidden = !shows;
  $("drill-shows").replaceChildren(h("strong", {}, "What this shows: "), shows || "");
  const others = drills.filter(d => d !== drill);
  $("drill-recent").hidden = !others.length;
  $("drill-recent").replaceChildren(h("span", {}, "Other walkthroughs:"), ...others.map(d =>
    h("button", {class: "ghost", type: "button", onclick: () => { drill = d; renderDrill(); refreshDrill(); }},
      d.view ? d.view.title : SCENARIOS[d.want.kind])));
  const body = $("drill-body"), nodes = [];
  if (down) nodes.push(h("div", {class: "outage"}, h("strong", {}, `Orchestrator unreachable for ${downFor()}s`),
    h("p", {}, "The dashboard is served by the orchestrator, so the picture is frozen at the last state it reported. This walkthrough continues when it answers again.")));
  if (!view) {
    nodes.push(h("div", {class: "armed"}, h("strong", {}, drill.desc ? (drill.offline ? "Waiting for the orchestrator" : "Unavailable") : "Waiting to start"),
      h("p", {}, drill.message), !drill.desc && h("p", {class: "hint"}, "This scenario starts by itself as soon as there is something to act on. Close this panel to cancel.")));
  } else if (view.waiting) {
    nodes.push(h("div", {class: "armed"}, h("strong", {}, "Armed"), h("p", {}, view.waiting),
      h("p", {class: "hint"}, "Launch a task if none is running. This panel follows the crash as soon as it happens.")));
  } else {
    nodes.push(h("ol", {class: "steps"}, view.steps.map(stepView)));
    if (view.summary) nodes.push(h("div", {class: "outcome"}, h("strong", {}, "Result"), h("p", {}, view.summary)));
  }
  body.replaceChildren(...nodes);
  const current = view && view.steps.find(step => step.state === "active");
  if (current && current.key !== drill.shown) {      // follow the step that is happening now
    drill.shown = current.key;
    const el = body.querySelector(`[data-step="${current.key}"]`);
    if (el) el.scrollIntoView({block: "nearest", behavior: calm ? "auto" : "smooth"});
  }
  if (view && view.summary && !drill.told) {         // show the result once, when it arrives
    drill.told = true;
    const el = body.querySelector(".outcome");
    if (el) el.scrollIntoView({block: "nearest", behavior: calm ? "auto" : "smooth"});
  }
  applyFocus();
}

// While the orchestrator does not answer, say so where it lives in the diagram.
function renderDown() {
  document.body.classList.toggle("orch-down", !!down && Date.now() - down > 1500);
  if (!down) return;
  $("connection").textContent = `Orchestrator unreachable for ${downFor()}s; retrying…`;
  if (state && Date.now() - down > 1500) $("node-orch").replaceChildren(
    h("p", {class: "eyebrow"}, "ORCHESTRATOR"), h("h3", {}, "Not running"),
    h("p", {class: "node-stat"}, h("strong", {}, `${downFor()}s`), " without an orchestrator"),
    h("p", {class: "node-stat"}, "The VMs and their agents keep working. Everything below is the last state it reported."));
  renderDrill();
}

$("drill-close").addEventListener("click", closeDrill);
$("drill-reopen").addEventListener("click", () => { drill = drills[0]; showDrill(); refreshDrill(); renderCards(); });

// Stop every run and empty the board, so the next launch starts a fresh round at Alice.
$("stop-all").addEventListener("click", async () => {
  await act("/clear_runs", {all: true});
  $("launch-seed").value = 1;
  $("launch-result").textContent = "Board cleared. Launch a new round when you are ready.";
});

// ---------------------------------------------------------------- launch form
$("launch").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.target.querySelector("button");
  const count = Number($("launch-count").value), seed = Number($("launch-seed").value);
  const name = $("launch-name").value.trim(), model = $("launch-model").value.trim();
  const instructions = $("launch-instructions").value.trim();
  button.disabled = true;
  try {
    for (let i = 0; i < count; i++) {
      const spec = {seed: seed + i};
      if (model) spec.model = model;
      if (instructions) spec.instructions = instructions;
      await api("/submit", {task_id: `${name}-${seed + i}`, spec});
    }
    $("launch-result").textContent = `Launched ${count} task${count === 1 ? "" : "s"} (${name}-${seed} … ${name}-${seed + count - 1}).`;
    $("launch-seed").value = seed + count;
    showError(null);
    await refresh();
  } catch (error) {
    $("launch-result").textContent = `Could not launch: ${error.message}`;
  } finally {
    button.disabled = false;
  }
});

async function refresh() {
  try {
    state = await api("/demo/state");
  } catch (error) {
    down = down || Date.now();
    renderDown();
    refreshDrill();
    return;
  }
  down = null;
  renderDown();
  byId = Object.fromEntries(state.tasks.map(task => [task.id, task]));
  $("connection").textContent = `Live · updated ${clock(state.now)}`;
  renderMetrics(); renderCards(); renderTopology(); renderTasks(); renderEvents();
  const running = state.tasks.filter(task => !ENDED.includes(task.status)).length;
  $("stop-all").disabled = !state.tasks.length;
  $("stop-all").textContent = running ? "Stop & clear all" : "Clear all";
  if (open && !byId[open.task]) { open = null; $("drawer").hidden = true; }
  refreshDrawer();
  refreshDrill();
}

renderLegend();
renderFlowLegend();
refresh();
setInterval(refresh, 1000);
