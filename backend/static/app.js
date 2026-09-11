"use strict";

/* IQCore front end. Talks to the same API the Streamlit app did — no backend
   change — so the contract lives entirely in renderPayload() below. */

// Selling price per token, in paise. The one number to change: every figure on
// screen is this times a token count Groq reported. 2p means a 5,000-token
// question costs Rs 100.
const PAISE_PER_TOKEN = 2;

const rupees = (t) =>
  "₹" + (((t || 0) * PAISE_PER_TOKEN) / 100).toLocaleString("en-IN",
    { minimumFractionDigits: 2, maximumFractionDigits: 2 });

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

const state = {
  history: [],      // [{role, content}] — exactly what the API expects
  payloads: [],     // one per answer, kept so earlier turns keep their tables
  busy: false,
};

/* ------------------------------------------------------------------ text */

const esc = (s) => String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

/* Just enough markdown for what the answers actually contain: bold, inline
   code, and bullet lists. Escaped first, so a backend that echoes a user string
   cannot inject markup. A parser library would be a dependency for three
   regexes. */
function markdown(src) {
  const lines = esc(src).split("\n");
  let html = "", list = false;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    // Pipe tables. The synthesis prompt asks for real values and the model
    // reaches for a table to give them, so this is ordinary output, not an
    // edge case — left unparsed it shows as a wall of pipes.
    if (/^\s*\|.*\|\s*$/.test(line) && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1] || "")) {
      const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const head = cells(line);
      let body = "";
      i += 2;
      for (; i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i]); i++) {
        body += "<tr>" + cells(lines[i]).map((c) =>
          "<td" + (isNum(c) ? ' class="num"' : "") + ">" + inline(c) + "</td>").join("") + "</tr>";
      }
      i--;
      if (list) { html += "</ul>"; list = false; }
      html += '<div class="tablewrap"><table class="md"><thead><tr>' +
        head.map((c) => "<th>" + inline(c) + "</th>").join("") +
        "</tr></thead><tbody>" + body + "</tbody></table></div>";
      continue;
    }

    const bullet = line.match(/^\s*[-*•]\s+(.*)$/);
    if (bullet) {
      if (!list) { html += "<ul>"; list = true; }
      html += "<li>" + inline(bullet[1]) + "</li>";
      continue;
    }
    if (list) { html += "</ul>"; list = false; }
    if (line.trim() === "") continue;
    html += "<p>" + inline(line) + "</p>";
  }
  if (list) html += "</ul>";
  return html;
}
const inline = (s) => s
  .replace(/`([^`]+)`/g, "<code>$1</code>")
  .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");

/* ---------------------------------------------------------------- reveal */

const observer = new IntersectionObserver((entries) => {
  for (const e of entries) {
    if (e.isIntersecting) { e.target.classList.add("in"); observer.unobserve(e.target); }
  }
}, { rootMargin: "0px 0px -40px 0px" });

function reveal(node) {
  node.classList.add("reveal");
  observer.observe(node);
  return node;
}

/* ----------------------------------------------------------------- graph */

const GRAPH_NODES = {
  classify:     { x: 176, y: 8,  label: "classify" },
  clarify:      { x: 8,   y: 74, label: "clarify" },
  simple_path:  { x: 176, y: 74, label: "simple_path" },
  complex_path: { x: 344, y: 74, label: "complex_path" },
};
const BRANCH = {
  clarify: "M232,44 L80,44 L80,74",
  simple_path: "M232,44 L232,74",
  complex_path: "M232,44 L384,74".replace("L384,74", "L384,44 L384,74"),
};

function graphSvg(states, caption) {
  const lit = (n) => states[n] === "active" || states[n] === "done";
  let s = `<svg viewBox="0 0 464 140" role="img" aria-label="Which path the question took">
    <defs><marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5"
      markerHeight="5" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" fill="#c9c9c6"/></marker></defs>`;
  for (const [name, d] of Object.entries(BRANCH)) {
    const on = lit(name);
    s += `<path d="${d}" fill="none" stroke="${on ? "#2f3437" : "#eaeaea"}"
      stroke-width="${on ? 1.5 : 1}" ${on ? "" : 'stroke-dasharray="4 4"'}
      marker-end="url(#ar)"/>`;
  }
  for (const [name, b] of Object.entries(GRAPH_NODES)) {
    const st = states[name] || "idle";
    const active = st === "active";
    const done = st === "done";
    const stroke = active ? "#2f3437" : done ? "#b9b9b5" : "#eaeaea";
    const fill = active ? "#f0efec" : "#ffffff";
    const ink = active ? "#111111" : done ? "#2f3437" : "#a5a49f";
    s += `<rect x="${b.x}" y="${b.y}" width="112" height="30" rx="7"
      fill="${fill}" stroke="${stroke}" stroke-width="${active ? 1.5 : 1}"/>
      <text x="${b.x + 56}" y="${b.y + 19}" text-anchor="middle" font-size="10.5"
      font-family="ui-monospace,SFMono-Regular,Menlo,monospace" fill="${ink}">${esc(b.label)}</text>`;
    if (active) {
      s += `<circle cx="${b.x + 100}" cy="${b.y + 10}" r="2.5" fill="#111">
        <animate attributeName="opacity" values="1;.2;1" dur="1.1s" repeatCount="indefinite"/></circle>`;
    }
  }
  if (caption) {
    s += `<text x="232" y="130" text-anchor="middle" font-size="10.5"
      font-family="ui-monospace,SFMono-Regular,Menlo,monospace" fill="#787774">${esc(caption)}</text>`;
  }
  return s + "</svg>";
}

/* --------------------------------------------------------------- treemap */
/* Slice-and-dice, ported from ceph_treemap.py: hosts become columns whose width
   is their share of capacity, OSDs become rows inside them. Area stays honest. */

function treemap(rows) {
  rows = (rows || []).filter((r) => (+r.size_GB || 0) > 0);
  if (!rows.length) return null;
  const W = 720, H = 300, PAD = 3, HEAD = 20;
  const byHost = {};
  for (const r of rows) (byHost[r.host || "?"] ||= []).push(r);
  const total = rows.reduce((a, r) => a + (+r.size_GB || 0), 0);
  if (total <= 0) return null;

  let s = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Ceph capacity by host and OSD">`;
  let x = 0;
  for (const host of Object.keys(byHost).sort()) {
    const osds = byHost[host].slice().sort((a, b) => (+b.size_GB) - (+a.size_GB));
    const hostTotal = osds.reduce((a, r) => a + (+r.size_GB || 0), 0);
    const colW = (hostTotal / total) * W;
    s += `<text x="${(x + colW / 2).toFixed(1)}" y="12" text-anchor="middle"
      font-size="11" font-weight="600" fill="#787774">${esc(host)} · ${Math.round(hostTotal).toLocaleString()} GB</text>`;
    let y = HEAD;
    for (const o of osds) {
      const size = +o.size_GB || 0;
      const h = hostTotal ? (size / hostTotal) * (H - HEAD) : 0;
      const up = o.up === undefined ? true : !!o.up;
      s += `<rect x="${(x + PAD).toFixed(1)}" y="${(y + PAD).toFixed(1)}"
        width="${Math.max(colW - 2 * PAD, 1).toFixed(1)}"
        height="${Math.max(h - 2 * PAD, 1).toFixed(1)}" rx="5"
        fill="${up ? "#2f3437" : "#9f2f2d"}" stroke="#ffffff" stroke-width="1"/>`;
      if (h > 34) {
        const cx = (x + colW / 2).toFixed(1);
        s += `<text x="${cx}" y="${(y + h / 2 - 3).toFixed(1)}" text-anchor="middle"
          font-size="11.5" font-weight="600" fill="#fff">${esc(o.osd)}</text>
          <text x="${cx}" y="${(y + h / 2 + 13).toFixed(1)}" text-anchor="middle"
          font-size="10" fill="#fff" opacity="0.8">${Math.round(size).toLocaleString()} GB${
            up ? "" : " · DOWN"}</text>`;
      } else if (h > 14) {
        s += `<text x="${(x + colW / 2).toFixed(1)}" y="${(y + h / 2 + 4).toFixed(1)}"
          text-anchor="middle" font-size="9.5" fill="#fff">${esc(o.osd)}</text>`;
      }
      y += h;
    }
    x += colW;
  }
  return s + "</svg>";
}

/* ---------------------------------------------------------------- tables */

const isNum = (v) => v !== null && v !== "" && !isNaN(Number(v));

function table(rows) {
  const cols = [...new Set(rows.flatMap((r) => Object.keys(r)))];
  const wrap = el("div", "tablewrap");
  const t = el("table");
  const thead = el("thead"), tr = el("tr");
  cols.forEach((c, i) => {
    const th = el("th", null, c);
    th.addEventListener("click", () => sort(t, i, th));
    tr.appendChild(th);
  });
  thead.appendChild(tr);
  t.appendChild(thead);

  const tb = el("tbody");
  rows.forEach((r, i) => {
    const row = el("tr");
    // Cascade the rows in. Capped so a 500-row table does not spend 13 seconds
    // arriving.
    row.style.setProperty("--i", Math.min(i, 18));
    for (const c of cols) {
      const v = r[c];
      const td = el("td", isNum(v) ? "num" : null,
        v === null || v === undefined ? "—" : String(v));
      row.appendChild(td);
    }
    tb.appendChild(row);
  });
  t.appendChild(tb);
  wrap.appendChild(t);
  return wrap;
}

function sort(t, index, th) {
  const dir = th.dataset.dir === "asc" ? "desc" : "asc";
  t.querySelectorAll("th").forEach((h) => { delete h.dataset.dir; h.removeAttribute("data-arrow"); });
  th.dataset.dir = dir;
  th.setAttribute("data-arrow", dir === "asc" ? "↑" : "↓");
  const tb = t.querySelector("tbody");
  const rows = [...tb.rows];
  rows.sort((a, b) => {
    const x = a.cells[index].textContent, y = b.cells[index].textContent;
    const cmp = isNum(x) && isNum(y) ? Number(x) - Number(y) : x.localeCompare(y);
    return dir === "asc" ? cmp : -cmp;
  });
  rows.forEach((r) => tb.appendChild(r));
}

/* --------------------------------------------------------------- payload */

function renderPayload(payload, into) {
  if (!payload) return;

  for (const note of payload.coverage_notes || []) {
    into.appendChild(el("div", "note", note));
  }
  for (const note of payload.guard_notes || []) {
    into.appendChild(el("div", "note", "Argument guard: " + note));
  }

  (payload.dataframes || []).forEach((frame) => {
    if (!Array.isArray(frame) || !frame.length) return;
    const card = el("div", "card");
    const head = el("div", "card-head");
    head.appendChild(el("span", "card-title", "Result"));
    head.appendChild(el("span", "tag", frame.length + " row" + (frame.length === 1 ? "" : "s")));
    card.appendChild(head);

    const cols = new Set(Object.keys(frame[0] || {}));
    if (["host", "osd", "size_GB"].every((c) => cols.has(c))) {
      const svg = treemap(frame);
      if (svg) {
        const box = el("div", "graph");
        box.innerHTML = svg;
        card.appendChild(box);
        card.appendChild(el("p", "rowcount",
          "Area is proportional to raw capacity. Red means the OSD is not up."));
      }
    }
    card.appendChild(table(frame));
    into.appendChild(card);
  });

  const t = payload.telemetry || {};
  const meta = el("div", "meta");
  if (t.served_from_cache) {
    meta.appendChild(el("span", null, "0 tokens"));
    meta.appendChild(el("span", "price", rupees(0)));
    meta.appendChild(el("span", null,
      "served from cache · " + (t.original_total_tokens || 0).toLocaleString() +
      " tokens / " + rupees(t.original_total_tokens) + " when first asked"));
  } else if ((t.calls || []).length) {
    meta.appendChild(el("span", null, t.total_tokens.toLocaleString() + " tokens"));
    meta.appendChild(el("span", "price", rupees(t.total_tokens)));
    meta.appendChild(el("span", null, Math.round(t.minute_budget_pct) + "% of a minute"));
    meta.appendChild(el("span", null, t.model_calls + " model call" + (t.model_calls === 1 ? "" : "s")));
    meta.appendChild(el("span", null, t.seconds.toFixed(1) + "s"));
    if (t.minute_budget_pct > 100) {
      meta.appendChild(el("span", "tag red", "over budget"));
    }
  }
  if (payload.complexity && payload.complexity !== "unknown") {
    meta.appendChild(el("span", "tag " + (payload.complexity === "simple" ? "green" : "blue"),
      payload.complexity));
  }
  if (meta.children.length) into.appendChild(meta);

  if ((t.by_operation || []).length) {
    into.appendChild(details("Token breakdown", (body) => {
      body.appendChild(table((t.by_operation || []).map((e) => ({
        Operation: e.label, Calls: e.calls, Tokens: e.total_tokens,
        Cost: rupees(e.total_tokens), Share: e.share_pct + "%",
      }))));
      body.appendChild(table((t.calls || []).map((c) => ({
        Operation: c.operation, Detail: c.detail || "—",
        Model: String(c.model).split("/").pop(),
        In: c.input_tokens, Out: c.output_tokens, Thinking: c.reasoning_tokens,
        Total: c.total_tokens, Cost: rupees(c.total_tokens), Seconds: c.seconds,
      }))));
      if (t.reasoning_tokens) {
        body.appendChild(el("p", "rowcount",
          t.reasoning_tokens.toLocaleString() + " of " + t.output_tokens.toLocaleString() +
          " output tokens were the model thinking before it answered — billed, " +
          "never shown in the reply."));
      }
      if ((t.unmeasured_calls || []).length) {
        body.appendChild(el("div", "note",
          t.unmeasured_calls.length + " call(s) reported no usage, so the total is a minimum: " +
          t.unmeasured_calls.join(", ")));
      }
    }));
  }

  into.appendChild(details("Agent reasoning", (body) => {
    body.appendChild(el("p", "rowcount",
      "Tools called: " + ((payload.tools_called || []).join(", ") || "none")));
    for (const entry of payload.tool_results_raw || []) {
      body.appendChild(el("p", "rowcount",
        (entry.tool || "unknown") + " · " + JSON.stringify(entry.args || {})));
      const pre = el("pre", "json");
      pre.textContent = JSON.stringify(entry.result || entry, null, 1);
      body.appendChild(pre);
    }
  }));
}

function details(label, build) {
  const d = el("details");
  d.appendChild(el("summary", null, label));
  const body = el("div", "body");
  build(body);
  d.appendChild(body);
  return d;
}

/* ----------------------------------------------------------------- spend */

// The plan's ceiling, in rupees. Representational only — nothing stops when it
// is reached, it is here to show a session against the budget it will be sold
// under. Move it server-side once a real plan exists per customer.
const SPEND_CEILING = 10000;

function updateSpend() {
  const tokens = state.payloads.reduce(
    (a, p) => a + (((p || {}).telemetry || {}).total_tokens || 0), 0);
  const n = state.payloads.length;
  const box = $("spend");
  box.innerHTML = "";
  box.className = "spend";
  if (!n) return;

  const spent = (tokens * PAISE_PER_TOKEN) / 100;
  const pct = (spent / SPEND_CEILING) * 100;
  // Colour is the warning, so it has to arrive before the number is alarming.
  if (pct >= 90) box.classList.add("over");
  else if (pct >= 65) box.classList.add("warn");

  box.appendChild(el("strong", null, rupees(tokens)));

  const meter = el("span", "meter");
  meter.setAttribute("role", "progressbar");
  meter.setAttribute("aria-valuenow", spent.toFixed(2));
  meter.setAttribute("aria-valuemin", "0");
  meter.setAttribute("aria-valuemax", String(SPEND_CEILING));
  meter.setAttribute("aria-label", "Spend against the plan ceiling");
  const fill = el("i");
  // A hair of width from the first question, so the bar reads as a bar rather
  // than an empty track: at 2 paise a token, one answer is 1% of the ceiling.
  fill.style.width = Math.max(Math.min(pct, 100), spent > 0 ? 1.5 : 0) + "%";
  meter.appendChild(fill);
  box.appendChild(meter);

  box.appendChild(el("span", "ceiling",
    pct < 1 ? "of ₹10,000" : pct.toFixed(pct < 10 ? 1 : 0) + "% of ₹10,000"));

  box.title = n + " answer" + (n === 1 ? "" : "s") + " · " +
    rupees(Math.round(tokens / n)) + " average · " +
    tokens.toLocaleString() + " tokens at " + PAISE_PER_TOKEN + "p each";
}

/* ------------------------------------------------------------------ chat */

/* Which system an answer came from, for the accent rail and the source pill.
   "mixed" when a question genuinely crossed two, which is worth showing. */
const SYSTEMS = {
  query_zabbix: ["zabbix", "Zabbix"],
  query_zammad: ["zammad", "Zammad"],
  query_prometheus: ["prometheus", "Prometheus"],
  query_ceph: ["ceph", "Ceph"],
  check_ceph_daemon_health: ["ceph", "Ceph daemons"],
  query_opensearch: ["opensearch", "OpenSearch"],
  correlate_problems_and_tickets: ["mixed", "Zabbix + Zammad"],
  lookup_ticket_context: ["mixed", "Zammad + Zabbix"],
  correlate_vm_storage_and_compute: ["mixed", "Ceph + Prometheus"],
};

function sourceOf(tools) {
  const hits = (tools || []).map((t) => SYSTEMS[t]).filter(Boolean);
  if (!hits.length) return null;
  const keys = new Set(hits.map((h) => h[0]));
  if (keys.size > 1) return ["mixed", [...new Set(hits.map((h) => h[1]))].join(" + ")];
  return hits[0];
}

function turnNode(question) {
  const turn = el("article", "turn live");
  const q = el("p", "q");
  q.appendChild(el("span", "who", "Asked"));
  q.appendChild(document.createTextNode(question));

  const answer = el("div", "answer");
  const graph = el("div", "graph");
  const status = el("div", "status");
  status.appendChild(el("span", "dot"));
  status.appendChild(el("span", null, "Working out which system to check…"));
  const source = el("div");
  const body = el("div", "a");
  const extras = el("div");
  answer.append(graph, status, source, body, extras);

  turn.append(q, answer);
  $("thread").appendChild(turn);
  return { turn, graph, status, source, answer: body, extras };
}

async function ask(question) {
  if (state.busy || !question.trim()) return;
  state.busy = true;
  $("send").disabled = true;
  $("empty").hidden = true;
  $("chips").innerHTML = "";

  const ui = turnNode(question);
  const nodes = {};
  let text = "", payload = null;
  ui.answer.innerHTML = '<span class="caret"></span>';
  ui.graph.innerHTML = graphSvg(nodes, "starting");
  window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });

  try {
    const res = await fetch("/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, conversation_history: state.history }),
    });
    if (!res.ok) throw new Error("HTTP " + res.status);

    // EventSource is GET-only and this endpoint is a POST, so the SSE frames
    // are read off the body stream by hand. They are newline-delimited
    // "data: {json}" blocks, which is the whole format we need.
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split("\n\n");
      buffer = frames.pop();
      for (const frame of frames) {
        const line = frame.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        const ev = JSON.parse(line.slice(6));

        if (ev.type === "token") {
          text += ev.text;
          ui.answer.innerHTML = markdown(text) + '<span class="caret"></span>';
        } else if (ev.type === "status") {
          ui.status.lastChild.textContent = ev.message;
        } else if (ev.type === "node") {
          nodes[ev.node] = ev.phase === "enter" ? "active" : "done";
          ui.graph.innerHTML = graphSvg(nodes, "");
        } else if (ev.type === "tool") {
          ui.graph.innerHTML = graphSvg(nodes, "running " + ev.tool);
        } else if (ev.type === "done") {
          payload = ev.payload;
        } else if (ev.type === "error") {
          text += (text ? "\n\n" : "") + ev.message;
          ui.answer.innerHTML = markdown(text);
        }
      }
    }
  } catch (err) {
    ui.answer.innerHTML = markdown("Could not reach the backend. " + err.message);
  }

  ui.status.remove();
  ui.turn.classList.remove("live");
  ui.answer.innerHTML = markdown(text || (payload && payload.final_answer) || "");
  ui.graph.innerHTML = graphSvg(nodes, "");

  if (payload) {
    // Colour the whole turn by the system that answered it, and name it above
    // the prose. The style brief already asks the model to open by naming what
    // it checked; this makes that legible without reading a word.
    const src = sourceOf(payload.tools_called);
    if (src) {
      ui.turn.classList.add("sys-" + src[0]);
      ui.source.appendChild(el("span", "source", src[1]));
    }
    renderPayload(payload, ui.extras);
    state.payloads.push(payload);
    state.history.push({ role: "user", content: question });
    state.history.push({ role: "assistant", content: text || payload.final_answer || "" });
    updateSpend();
    renderChips(payload.suggestions || []);
  }
  reveal(ui.turn).classList.add("in");
  state.busy = false;
  $("send").disabled = false;
  $("q").focus();
}

function renderChips(list) {
  const box = $("chips");
  box.innerHTML = "";
  list.forEach((s, i) => {
    const b = el("button", "chip", s);
    b.type = "button";
    b.style.animationDelay = i * 80 + "ms";
    b.addEventListener("click", () => { box.innerHTML = ""; ask(s); });
    box.appendChild(b);
  });
}

/* ----------------------------------------------------------- connections */

let CONFIG = null;

async function loadConfig() {
  const status = $("config-status");
  status.innerHTML = "";
  try {
    CONFIG = await (await fetch("/config")).json();
  } catch (err) {
    status.appendChild(el("div", "note bad",
      "Cannot reach the backend. " + err.message));
    return;
  }
  if (!CONFIG.configured) {
    status.appendChild(el("div", "note", "Nothing is configured yet — add at least the Groq key and one system."));
  }
  $("config-path").innerHTML = "Stored at <code>" + esc(CONFIG.path) + "</code>";

  const groups = $("groups");
  groups.innerHTML = "";
  CONFIG.groups.forEach((g, i) => {
    const d = el("details", "group " + hueOf(g.system));
    d.dataset.system = g.system;
    d.style.setProperty("--i", i);
    if (!CONFIG.configured) d.open = true;
    const sum = el("summary");
    sum.appendChild(el("span", "sys", g.system));
    sum.appendChild(el("span", "desc", " — " + g.description));
    sum.appendChild(el("span", "state"));
    d.appendChild(sum);
    const fields = el("div", "fields");
    for (const f of g.fields) {
      const current = CONFIG.values[f.key] || "";
      const label = el("label");
      label.appendChild(el("span", "lab", f.label));
      const input = el("input");
      input.type = f.secret ? "password" : "text";
      input.name = f.key;
      // Secrets come back masked. Putting the mask in the value would save the
      // bullet characters back as the password, so secrets start empty and
      // blank means "unchanged".
      input.value = f.secret ? "" : current;
      input.placeholder = f.secret && current ? "unchanged (" + current + ")" : f.placeholder;
      label.appendChild(input);
      fields.appendChild(label);
    }
    d.appendChild(fields);
    groups.appendChild(d);
  });
}

/* The group's colour, keyed off the system name the API already sends. Same
   hue the answers use, so a system looks the same everywhere. */
function hueOf(system) {
  const key = String(system).toLowerCase();
  return ["zabbix", "zammad", "prometheus", "ceph", "opensearch", "groq"]
    .includes(key) ? "sys-" + key : "sys-mixed";
}

function entered() {
  const values = {};
  for (const input of $("config-form").querySelectorAll("input")) {
    if (input.value.trim()) values[input.name] = input.value.trim();
  }
  return values;
}

function showResults(results) {
  const box = $("results");
  box.innerHTML = "";
  Object.entries(results || {}).forEach(([system, outcome], i) => {
    const row = el("div", "result " + hueOf(system));
    row.style.setProperty("--i", i);
    row.appendChild(el("span", "tag " + (outcome.ok ? "green" : "red"),
      outcome.ok ? "ok" : "fail"));
    row.appendChild(el("span", "sys", system));
    row.appendChild(el("span", "msg", outcome.message));
    box.appendChild(row);

    // Mirror the outcome onto the group itself. Six systems is enough that
    // scrolling to a list at the bottom to find the broken one is the wrong
    // way round.
    const group = $("groups").querySelector('[data-system="' + system + '"]');
    if (group) {
      group.classList.toggle("ok", !!outcome.ok);
      group.classList.toggle("bad", !outcome.ok);
      group.querySelector(".state").textContent = outcome.ok ? "ok" : "failed";
      if (!outcome.ok) group.open = true;
    }
  });
}

async function post(path, body, busyLabel) {
  const busy = $("config-busy");
  busy.textContent = busyLabel;
  busy.hidden = false;
  $("test").disabled = $("save").disabled = true;
  try {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return await res.json();
  } finally {
    busy.hidden = true;
    $("test").disabled = $("save").disabled = false;
  }
}

/* ---------------------------------------------------------------- router */

function show(page) {
  for (const tab of document.querySelectorAll(".tab")) {
    const on = tab.dataset.page === page;
    tab.toggleAttribute("aria-current", on);
    if (on) tab.setAttribute("aria-current", "page");
  }
  $("page-chat").hidden = page !== "chat";
  $("page-connections").hidden = page !== "connections";
  $("composer").hidden = page !== "chat";
  if (page === "connections") loadConfig();
  if (location.hash.slice(1) !== page) location.hash = page;
}

/* ------------------------------------------------------------------ wire */

document.querySelectorAll(".tab").forEach((tab) =>
  tab.addEventListener("click", () => show(tab.dataset.page)));
window.addEventListener("hashchange", () => show(location.hash.slice(1) || "chat"));

$("ask").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("q").value;
  $("q").value = "";
  $("q").style.height = "auto";
  ask(q);
});

$("q").addEventListener("input", (e) => {
  e.target.style.height = "auto";
  e.target.style.height = Math.min(e.target.scrollHeight, 180) + "px";
});
$("q").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("ask").requestSubmit();
  }
});

$("test").addEventListener("click", async () => {
  const out = await post("/config/test", { values: entered(), systems: [] }, "Checking");
  showResults(out.results);
});

$("config-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const out = await post("/config", { values: entered(), systems: [] }, "Saving");
  showResults(out.results);
  const status = $("config-status");
  status.innerHTML = "";
  status.appendChild(el("div", "note ok",
    (out.changed || []).length
      ? "Saved " + out.changed.length + " setting(s). Cleared: " +
        (out.caches_cleared || []).join(", ") + "."
      : "No changes to save."));
  loadConfig();
});

// Tell a new user where the credentials live, without blocking the chat on it:
// an unreachable backend is reported per question, not as a dead page.
fetch("/config").then((r) => r.json()).then((c) => {
  if (!c.configured) {
    $("unconfigured").appendChild(el("div", "note",
      "No systems are configured yet — open Connections to add them."));
  }
}).catch(() => {});

show(location.hash.slice(1) || "chat");

/* ------------------------------------------------------------------ gate */
/* A curtain over the interface, not a lock on the API — see auth_store.py. The
   "signed in" flag lives in sessionStorage, so a reload inside the same tab
   does not re-prompt but a new tab or a restart does. */

let signingUp = false;

function gateMode(signup) {
  signingUp = signup;
  $("gate-title").textContent = signup ? "Create an account" : "Sign in";
  $("gate-submit").textContent = signup ? "Create account" : "Sign in";
  $("gate-switch-text").textContent = signup ? "Already have an account?" : "No account yet?";
  $("gate-switch").textContent = signup ? "Sign in" : "Create one";
  $("gate-pass").autocomplete = signup ? "new-password" : "current-password";
  $("gate-error").hidden = true;
}

function openApp(username) {
  try { sessionStorage.setItem("iqcore_user", username); } catch (e) {}
  $("gate").hidden = true;
  $("q").focus();
}

$("gate-switch").addEventListener("click", () => gateMode(!signingUp));

$("gate-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("gate-error");
  err.hidden = true;
  $("gate-submit").disabled = true;
  try {
    const res = await fetch(signingUp ? "/auth/signup" : "/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: $("gate-user").value, password: $("gate-pass").value }),
    });
    const out = await res.json();
    if (out.ok) {
      openApp(out.username);
    } else {
      err.textContent = out.error || "That did not work.";
      err.hidden = false;
    }
  } catch (ex) {
    err.textContent = "Could not reach the server. " + ex.message;
    err.hidden = false;
  } finally {
    $("gate-submit").disabled = false;
  }
});

(async () => {
  if (sessionStorage.getItem("iqcore_user")) { $("q").focus(); return; }
  $("gate").hidden = false;
  // A fresh install has no accounts, so open on sign-up rather than showing a
  // login nobody can pass.
  try {
    const { accounts } = await (await fetch("/auth/status")).json();
    gateMode(accounts === 0);
    if (accounts === 0) {
      $("gate-lede").textContent =
        "No accounts exist yet. The first one you create is yours.";
    }
  } catch (e) {
    gateMode(false);
  }
  $("gate-user").focus();
})();
