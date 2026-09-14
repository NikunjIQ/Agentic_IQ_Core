"use strict";

/* IQCore front end. Everything it knows comes from the API: answers from
   /chat/stream, the account and its balance from /me, settings from /config.
   The session is an HttpOnly cookie the page never sees; it only learns it has
   been signed out when a request comes back 401. */

// Selling price per token, in paise. Replaced by the server's figure from /me
// once signed in — the server is what actually charges.
let PAISE_PER_TOKEN = 2;

const money = (paise) =>
  "₹" + ((paise || 0) / 100).toLocaleString("en-IN",
    { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const rupees = (tokens) => money((tokens || 0) * PAISE_PER_TOKEN);

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
  me: null,         // /me: who is signed in, and their balance
  conversationId: null,   // the saved conversation this thread belongs to
  pins: [],         // pinned questions, for the stars and the drawer
  busy: false,
};

/* Every call that needs a session goes through here. A 401 means the cookie
   expired or was never set, and the only useful response is the sign-in card. */
async function api(path, options) {
  const res = await fetch(path, options);
  if (res.status === 401) {
    showGate();
    throw new Error("Signed out");
  }
  return res;
}

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
  // Colours live in app.css (.g-*), not here, so dark mode can restyle them.
  const lit = (n) => states[n] === "active" || states[n] === "done";
  let s = `<svg class="flow" viewBox="0 0 464 140" role="img" aria-label="Which path the question took">
    <defs><marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5"
      markerHeight="5" orient="auto-start-reverse">
      <path class="g-arrow" d="M0,0 L10,5 L0,10 z"/></marker></defs>`;
  for (const [name, d] of Object.entries(BRANCH)) {
    s += `<path class="g-edge${lit(name) ? " on" : ""}" d="${d}" marker-end="url(#ar)"/>`;
  }
  for (const [name, b] of Object.entries(GRAPH_NODES)) {
    const st = states[name] || "idle";
    s += `<g class="g-node ${st}"><rect x="${b.x}" y="${b.y}" width="112" height="30" rx="7"/>
      <text x="${b.x + 56}" y="${b.y + 19}" text-anchor="middle">${esc(b.label)}</text>`;
    if (st === "active") {
      s += `<circle cx="${b.x + 100}" cy="${b.y + 10}" r="2.5">
        <animate attributeName="opacity" values="1;.2;1" dur="1.1s" repeatCount="indefinite"/></circle>`;
    }
    s += `</g>`;
  }
  if (caption) {
    s += `<text class="g-caption" x="232" y="130" text-anchor="middle">${esc(caption)}</text>`;
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
      font-size="11" font-weight="600" class="tm-head">${esc(host)} · ${Math.round(hostTotal).toLocaleString()} GB</text>`;
    let y = HEAD;
    for (const o of osds) {
      const size = +o.size_GB || 0;
      const h = hostTotal ? (size / hostTotal) * (H - HEAD) : 0;
      const up = o.up === undefined ? true : !!o.up;
      s += `<rect x="${(x + PAD).toFixed(1)}" y="${(y + PAD).toFixed(1)}"
        width="${Math.max(colW - 2 * PAD, 1).toFixed(1)}"
        height="${Math.max(h - 2 * PAD, 1).toFixed(1)}" rx="5"
        class="${up ? "tm-up" : "tm-down"}"/>`;
      if (h > 34) {
        const cx = (x + colW / 2).toFixed(1);
        s += `<text x="${cx}" y="${(y + h / 2 - 3).toFixed(1)}" text-anchor="middle"
          font-size="11.5" font-weight="600" class="tm-label">${esc(o.osd)}</text>
          <text x="${cx}" y="${(y + h / 2 + 13).toFixed(1)}" text-anchor="middle"
          font-size="10" class="tm-label" opacity="0.8">${Math.round(size).toLocaleString()} GB${
            up ? "" : " · DOWN"}</text>`;
      } else if (h > 14) {
        s += `<text x="${(x + colW / 2).toFixed(1)}" y="${(y + h / 2 + 4).toFixed(1)}"
          text-anchor="middle" font-size="9.5" class="tm-label">${esc(o.osd)}</text>`;
      }
      y += h;
    }
    x += colW;
  }
  return s + "</svg>";
}

/* ---------------------------------------------------------------- charts */
/* Picked from a result's columns, like the treemap: any tool that returns the
   right shape gets a chart without knowing charts exist. Hand-drawn SVG, no
   library. Colours come from CSS, so both themes and every system hue work. */

const fmt = (v) => Math.abs(v) >= 1000 ? Math.round(v).toLocaleString()
  : (Math.round(v * 100) / 100).toLocaleString();

// Numbers that name things rather than measure them. Charting ticket ids by
// size is noise that looks like data.
const ID_LIKE = /(^|_)(id|ids|number|nr|no|port|pid|uid|hostid|ticket|year)$/i;
// Columns a reader most likely wants on the axis, tried first.
const MAGNITUDE = /value|used|stored|size|count|total|percent|pct|gb|bytes|tokens|objects|load|cpu|memory|spent|paise/i;

function numericColumns(rows) {
  const cols = Object.keys(rows[0] || {}).filter((c) => {
    if (ID_LIKE.test(c)) return false;
    const vals = rows.map((r) => r[c]).filter((v) => v !== null && v !== "" && v !== undefined);
    // A constant column says nothing, and one mixed with text is not a measure.
    return vals.length && vals.every(isNum) && new Set(vals.map(String)).size > 1;
  });
  return cols.sort((a, b) => MAGNITUDE.test(b) - MAGNITUDE.test(a));
}

function textColumn(rows, exclude) {
  return Object.keys(rows[0] || {}).find((c) =>
    !exclude.includes(c) && !ID_LIKE.test(c) && rows.some((r) => r[c] != null && !isNum(r[c])));
}

function barChart(rows, labelCol, valueCol) {
  const top = rows.map((r) => ({ label: String(r[labelCol] ?? "—"), value: Number(r[valueCol]) || 0 }))
    .sort((a, b) => b.value - a.value).slice(0, 15);
  const max = Math.max(...top.map((d) => Math.abs(d.value)), 1);
  const W = 640, ROW = 24, LABEL = 170, H = top.length * ROW + 4;
  let s = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img"
    aria-label="${esc(valueCol)} by ${esc(labelCol)}">`;
  top.forEach((d, i) => {
    const y = i * ROW + 2;
    const w = Math.max(((W - LABEL - 70) * Math.abs(d.value)) / max, 1);
    s += `<text class="c-label" x="${LABEL - 10}" y="${y + 15}" text-anchor="end">${esc(d.label.slice(0, 26))}</text>
      <rect class="c-bar" x="${LABEL}" y="${y + 4}" width="${w.toFixed(1)}" height="${ROW - 9}" rx="3"
        style="--d:${i * 30}ms"><title>${esc(d.label)}: ${fmt(d.value)}</title></rect>
      <text class="c-value" x="${(LABEL + w + 8).toFixed(1)}" y="${y + 15}">${fmt(d.value)}</text>`;
  });
  return s + "</svg>";
}

/* Bars need a label column and something to measure. The "Chart by" select
   lets the reader pick the measure: the Ceph pools table has four. */
function attachBarChart(card, rows) {
  if (rows.length < 2) return;
  const nums = numericColumns(rows);
  const label = textColumn(rows, nums);
  if (!nums.length || !label) return;

  const head = el("div", "chart-head");
  const box = el("div", "chartbox");
  const more = el("span", "rowcount", rows.length > 15 ? "Top 15 of " + rows.length : "");
  if (nums.length > 1) {
    const pick = el("select", "chart-pick");
    for (const c of nums) pick.appendChild(new Option(c, c));
    pick.addEventListener("change", () => { box.innerHTML = barChart(rows, label, pick.value); });
    const by = el("label", "chart-by");
    by.appendChild(el("span", null, "Chart by"));
    by.appendChild(pick);
    head.appendChild(by);
  }
  head.appendChild(more);
  card.append(head, box);
  box.innerHTML = barChart(rows, label, nums[0]);
}

/* Lines: rows of {time, value, series}. The legend doubles as the hover
   readout — every series' value at the moment under the pointer. */
function lineChart(rows, box) {
  const bySeries = {};
  for (const r of rows) (bySeries[r.series || "value"] ||= []).push(r);
  const names = Object.keys(bySeries).slice(0, 8);
  const times = [...new Set(rows.map((r) => r.time))].sort();
  const t0 = Date.parse(times[0]);
  const t1 = Date.parse(times[times.length - 1]);
  const vals = rows.map((r) => Number(r.value));
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (hi === lo) { hi += 1; lo -= 1; }
  const unit = rows[0].unit || "";
  const W = 640, H = 220, L = 46, R = 12, T = 12, B = 26;
  const x = (t) => L + ((Date.parse(t) - t0) / Math.max(t1 - t0, 1)) * (W - L - R);
  const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
  const clock = (t) => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const latest = (n) => fmt(Number(bySeries[n][bySeries[n].length - 1].value)) + unit;

  let s = `<svg class="chart line" viewBox="0 0 ${W} ${H}" role="img"
    aria-label="${names.length} series over time">`;
  for (const f of [0, 0.5, 1]) {
    const v = lo + (hi - lo) * f, yy = y(v).toFixed(1);
    s += `<line class="c-grid" x1="${L}" x2="${W - R}" y1="${yy}" y2="${yy}"/>
      <text class="c-axis" x="${L - 6}" y="${(+yy + 3).toFixed(1)}" text-anchor="end">${fmt(v)}</text>`;
  }
  s += `<text class="c-axis" x="${L}" y="${H - 8}">${clock(times[0])}</text>
    <text class="c-axis" x="${W - R}" y="${H - 8}" text-anchor="end">${clock(times[times.length - 1])}</text>`;
  names.forEach((n, i) => {
    const pts = bySeries[n].map((r) => `${x(r.time).toFixed(1)},${y(Number(r.value)).toFixed(1)}`).join(" ");
    s += `<polyline class="c-line s${i}" points="${pts}"/>`;
  });
  s += `<line class="c-cross" x1="0" x2="0" y1="${T}" y2="${H - B}" visibility="hidden"/></svg>`;
  box.innerHTML = s;

  const legend = el("div", "legend");
  const when = el("span", "when", "latest");
  legend.appendChild(when);
  const cells = names.map((n, i) => {
    const key = el("span", "key");
    key.appendChild(el("i", "swatch s" + i));
    key.appendChild(el("span", null, n));
    const v = el("b", null, latest(n));
    key.appendChild(v);
    legend.appendChild(key);
    return v;
  });
  box.appendChild(legend);

  const svg = box.querySelector("svg");
  const cross = svg.querySelector(".c-cross");
  svg.addEventListener("pointermove", (e) => {
    const r = svg.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * W;
    if (px < L || px > W - R) return;
    const t = t0 + ((px - L) / (W - L - R)) * (t1 - t0);
    const near = times.reduce((a, b) =>
      Math.abs(Date.parse(b) - t) < Math.abs(Date.parse(a) - t) ? b : a);
    cross.setAttribute("x1", x(near));
    cross.setAttribute("x2", x(near));
    cross.setAttribute("visibility", "visible");
    when.textContent = clock(near);
    names.forEach((n, i) => {
      const p = bySeries[n].find((row) => row.time === near);
      cells[i].textContent = p ? fmt(Number(p.value)) + unit : "—";
    });
  });
  svg.addEventListener("pointerleave", () => {
    cross.setAttribute("visibility", "hidden");
    when.textContent = "latest";
    names.forEach((n, i) => { cells[i].textContent = latest(n); });
  });
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
      card.appendChild(table(frame));
    } else if (cols.has("time") && cols.has("value")) {
      // A time series: the chart is the answer, the points are the appendix.
      const box = el("div", "chartbox");
      card.appendChild(box);
      lineChart(frame, box);
      card.appendChild(details(frame.length + " data points",
        (body) => body.appendChild(table(frame))));
    } else {
      attachBarChart(card, frame);
      card.appendChild(table(frame));
    }
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
    if (payload.trimmed) {
      body.appendChild(el("p", "rowcount",
        "The raw tool output isn't kept with saved conversations."));
    }
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
/* The meter shows the balance the server holds: what an admin allocated, and
   what every answer so far has cost. It survives a reload and a new tab because
   it no longer lives in the page. Still representational — nothing is refused
   when it fills. */

async function refreshMe() {
  try {
    const res = await api("/me");
    state.me = await res.json();
  } catch (e) {
    return;
  }
  PAISE_PER_TOKEN = state.me.paise_per_token || PAISE_PER_TOKEN;
  $("who").textContent = state.me.username;
  for (const node of document.querySelectorAll("[data-admin]")) {
    node.hidden = state.me.role !== "admin";
  }
  updateSpend();
}

function updateSpend() {
  const me = state.me;
  const box = $("spend");
  box.innerHTML = "";
  box.className = "spend";
  box.removeAttribute("title");
  if (!me) return;

  const allocated = me.allocated_paise || 0;
  const pct = allocated > 0 ? (me.spent_paise / allocated) * 100 : (me.spent_paise > 0 ? 100 : 0);
  // Colour is the warning, so it has to arrive before the number is alarming.
  if (pct >= 90) box.classList.add("over");
  else if (pct >= 65) box.classList.add("warn");

  box.appendChild(el("strong", null, money(me.spent_paise)));

  const meter = el("span", "meter");
  meter.setAttribute("role", "progressbar");
  meter.setAttribute("aria-valuenow", String(me.spent_paise / 100));
  meter.setAttribute("aria-valuemin", "0");
  meter.setAttribute("aria-valuemax", String(allocated / 100));
  meter.setAttribute("aria-label", "Spend against your allocation");
  const fill = el("i");
  // A hair of width from the first answer, so the bar reads as a bar rather
  // than an empty track.
  fill.style.width = Math.max(Math.min(pct, 100), me.spent_paise > 0 ? 1.5 : 0) + "%";
  meter.appendChild(fill);
  box.appendChild(meter);

  const ceiling = "₹" + (allocated / 100).toLocaleString("en-IN", { maximumFractionDigits: 0 });
  box.appendChild(el("span", "ceiling",
    pct < 1 ? "of " + ceiling : pct.toFixed(pct < 10 ? 1 : 0) + "% of " + ceiling));

  box.title = me.answers + " answer" + (me.answers === 1 ? "" : "s") + " · " +
    money(me.remaining_paise) + " remaining · " + PAISE_PER_TOKEN + "p per token";
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
  const pin = el("button", "pin");
  pin.type = "button";
  pin.dataset.q = question;
  pin.innerHTML = STAR;
  markPin(pin, state.pins.some((p) => p.question === question));
  pin.addEventListener("click", () => togglePin(question));
  q.appendChild(pin);

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
    const res = await api("/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, conversation_history: state.history,
                             conversation_id: state.conversationId }),
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
    // Appended to the text, not written straight into the answer: the line
    // below re-renders the answer from `text`, and used to wipe this message.
    text += (text ? "\n\n" : "") + (err.message === "Signed out"
      ? "Your session ended — sign in again to continue."
      : "Could not reach the backend. " + err.message);
  }

  ui.status.remove();
  ui.turn.classList.remove("live");
  ui.answer.innerHTML = markdown(text || (payload && payload.final_answer) || "");
  ui.graph.innerHTML = graphSvg(nodes, "");

  if (payload) {
    if (payload.conversation_id) state.conversationId = payload.conversation_id;
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
    refreshMe();        // the server has already charged this answer
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
    CONFIG = await (await api("/config")).json();
  } catch (err) {
    if (err.message !== "Signed out") {
      status.appendChild(el("div", "note bad", "Cannot reach the backend. " + err.message));
    }
    return;
  }
  if (!CONFIG.configured) {
    status.appendChild(el("div", "note",
      "Nothing is connected to your account yet — fill in the systems you use. " +
      "A section left empty stays disconnected."));
  }
  $("config-path").innerHTML = "Stored in <code>" + esc(CONFIG.path) + "</code>";

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
    const res = await api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return await res.json();
  } catch (e) {
    return {};
  } finally {
    busy.hidden = true;
    $("test").disabled = $("save").disabled = false;
  }
}

/* ---------------------------------------------------------------- router */

function show(page) {
  // The server refuses /admin to anyone else; this just avoids a dead page.
  if (page === "users" && !(state.me && state.me.role === "admin")) page = "chat";
  for (const tab of document.querySelectorAll(".tab[data-page]")) {
    const on = tab.dataset.page === page;
    tab.toggleAttribute("aria-current", on);
    if (on) tab.setAttribute("aria-current", "page");
  }
  $("page-chat").hidden = page !== "chat";
  $("page-connections").hidden = page !== "connections";
  $("page-users").hidden = page !== "users";
  $("composer").hidden = page !== "chat";
  if (page === "connections") loadConfig();
  if (page === "users") loadUsers();
  if (location.hash.slice(1) !== page) location.hash = page;
}

/* ------------------------------------------------------------------ wire */

document.querySelectorAll(".tab[data-page]").forEach((tab) =>
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

/* ----------------------------------------------------------------- theme */
/* An explicit choice wins; none means follow the OS. A per-browser
   preference, so localStorage rather than the account. */

function currentTheme() {
  return document.documentElement.dataset.theme ||
    (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}

$("theme").addEventListener("click", () => {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("iqcore_theme", next); } catch (e) {}
});

/* --------------------------------------------------------- conversations */
/* Every answered question is kept on the server, per account. Reopening a
   conversation redraws it with the same functions that drew it first time. */

const STAR = `<svg width="15" height="15" viewBox="0 0 16 16" aria-hidden="true"><path
  d="M8 1.8l1.9 3.9 4.3.6-3.1 3 .7 4.3L8 11.6l-3.8 2 .7-4.3-3.1-3 4.3-.6z"
  stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>`;

function ago(stamp) {
  const s = (Date.now() - Date.parse(String(stamp).replace(" ", "T") + "Z")) / 1000;
  if (s < 90) return "just now";
  if (s < 3600) return Math.round(s / 60) + " min ago";
  if (s < 86400) return Math.round(s / 3600) + " h ago";
  return Math.round(s / 86400) + " d ago";
}

function markPin(button, on) {
  button.classList.toggle("on", on);
  button.setAttribute("aria-pressed", String(on));
  button.setAttribute("aria-label", on ? "Unpin this question" : "Pin this question");
}

function syncPinButtons() {
  const pinned = new Set(state.pins.map((p) => p.question));
  for (const b of document.querySelectorAll(".pin")) markPin(b, pinned.has(b.dataset.q));
}

async function loadPins() {
  try {
    state.pins = (await (await api("/favourites")).json()).favourites || [];
  } catch (e) {
    return;
  }
  const list = $("pins");
  list.innerHTML = "";
  $("pins-empty").hidden = state.pins.length > 0;
  for (const p of state.pins) {
    const li = el("li");
    const go = el("button", "drawer-item");
    go.type = "button";
    go.title = "Ask this again";
    go.appendChild(el("span", null, p.question));
    go.addEventListener("click", () => { closeDrawer(); show("chat"); ask(p.question); });
    const x = el("button", "drawer-x", "×");
    x.type = "button";
    x.setAttribute("aria-label", "Unpin");
    x.addEventListener("click", async () => {
      try { await api("/favourites/" + p.id, { method: "DELETE" }); } catch (e) { return; }
      loadPins();
    });
    li.append(go, x);
    list.appendChild(li);
  }
  syncPinButtons();
}

async function togglePin(question) {
  const existing = state.pins.find((p) => p.question === question);
  try {
    if (existing) {
      await api("/favourites/" + existing.id, { method: "DELETE" });
    } else {
      await api("/favourites", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question }),
      });
    }
  } catch (e) {
    return;
  }
  loadPins();
}

async function loadConversations() {
  let items = [];
  try {
    items = (await (await api("/conversations")).json()).conversations || [];
  } catch (e) {
    return;
  }
  const list = $("convos");
  list.innerHTML = "";
  $("convos-empty").hidden = items.length > 0;
  items.forEach((c, i) => {
    const li = el("li", c.id === state.conversationId ? "current" : null);
    li.style.setProperty("--i", Math.min(i, 18));
    const go = el("button", "drawer-item");
    go.type = "button";
    go.appendChild(el("span", null, c.title));
    go.appendChild(el("small", null,
      c.turns + " question" + (c.turns === 1 ? "" : "s") + " · " + ago(c.updated_at)));
    go.addEventListener("click", () => openConversation(c.id));
    const x = el("button", "drawer-x", "×");
    x.type = "button";
    x.setAttribute("aria-label", "Delete this conversation");
    x.addEventListener("click", async () => {
      try { await api("/conversations/" + c.id, { method: "DELETE" }); } catch (e) { return; }
      if (c.id === state.conversationId) newConversation();
      loadConversations();
    });
    li.append(go, x);
    list.appendChild(li);
  });
}

function clearThread() {
  state.history = [];
  state.payloads = [];
  $("thread").innerHTML = "";
  $("chips").innerHTML = "";
  $("empty").hidden = false;
}

function newConversation() {
  state.conversationId = null;
  clearThread();
  closeDrawer();
  show("chat");
  $("q").focus();
}

function drawStoredTurn(t) {
  const ui = turnNode(t.question);
  ui.status.remove();
  ui.turn.classList.remove("live");
  ui.answer.innerHTML = markdown(t.answer);
  const p = t.payload || {};
  const src = sourceOf(p.tools_called);
  if (src) {
    ui.turn.classList.add("sys-" + src[0]);
    ui.source.appendChild(el("span", "source", src[1]));
  }
  renderPayload(p, ui.extras);
  ui.turn.classList.add("reveal", "in");
  state.payloads.push(p);
  // Restored so a follow-up ("and on server03?") still has its context.
  state.history.push({ role: "user", content: t.question },
                     { role: "assistant", content: t.answer });
}

async function openConversation(id) {
  let data;
  try {
    data = await (await api("/conversations/" + id)).json();
  } catch (e) {
    return;
  }
  if (!data.turns) return;
  clearThread();
  state.conversationId = id;
  $("empty").hidden = true;
  data.turns.forEach(drawStoredTurn);
  closeDrawer();
  show("chat");
  window.scrollTo({ top: document.body.scrollHeight });
}

function openDrawer() {
  $("drawer").hidden = false;
  $("scrim").hidden = false;
  loadPins();
  loadConversations();
}

function closeDrawer() {
  $("drawer").hidden = true;
  $("scrim").hidden = true;
}

$("drawer-open").addEventListener("click", () =>
  ($("drawer").hidden ? openDrawer() : closeDrawer()));
$("drawer-close").addEventListener("click", closeDrawer);
$("scrim").addEventListener("click", closeDrawer);
$("new-chat").addEventListener("click", newConversation);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("drawer").hidden) closeDrawer();
});

/* ----------------------------------------------------------------- users */
/* Admin only: every account's allocation and spend. The server enforces it
   (403 for anyone else); hiding the tab only saves a dead end. */

async function loadUsers() {
  const status = $("users-status");
  status.innerHTML = "";
  let data;
  try {
    const res = await api("/admin/credits");
    if (res.status === 403) { show("chat"); return; }
    data = await res.json();
  } catch (e) {
    return;
  }

  const body = $("users-body");
  body.innerHTML = "";
  data.users.forEach((u, i) => {
    const row = el("tr");
    row.style.setProperty("--i", Math.min(i, 18));
    row.appendChild(el("td", null, u.username));
    row.appendChild(el("td", null, u.email || "—"));
    const role = el("td");
    role.appendChild(el("span", "tag" + (u.role === "admin" ? " blue" : ""), u.role));
    row.appendChild(role);

    const cell = el("td");
    const input = el("input");
    input.type = "number";
    input.min = "0";
    input.step = "500";
    input.value = String(u.allocated_paise / 100);
    input.setAttribute("aria-label", "Allocation in rupees for " + u.username);
    cell.appendChild(input);
    row.appendChild(cell);

    row.appendChild(el("td", "num", money(u.spent_paise)));
    row.appendChild(el("td", "num" + (u.remaining_paise < 0 ? " neg" : ""),
      money(u.remaining_paise)));
    row.appendChild(el("td", "num", String(u.answers)));

    const act = el("td");
    const save = el("button", "btn ghost small", "Save");
    save.type = "button";
    save.addEventListener("click", () => saveAllocation(u, input, save));
    act.appendChild(save);
    row.appendChild(act);
    body.appendChild(row);
  });
}

async function saveAllocation(u, input, button) {
  const status = $("users-status");
  status.innerHTML = "";
  button.disabled = true;
  try {
    const res = await api("/admin/credits", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: u.user_id, allocated_rupees: Number(input.value) }),
    });
    const out = await res.json();
    if (!res.ok) throw new Error(out.detail || "Could not save.");
    status.appendChild(el("div", "note ok", "Updated " + u.username + "'s allocation."));
    loadUsers();
    refreshMe();
  } catch (e) {
    if (e.message !== "Signed out") status.appendChild(el("div", "note bad", e.message));
  } finally {
    button.disabled = false;
  }
}

// Point a new user at Connections, without blocking the chat on it.
async function checkConfigured() {
  $("unconfigured").innerHTML = "";
  try {
    const c = await (await api("/config")).json();
    if (!c.configured) {
      $("unconfigured").appendChild(el("div", "note",
        "No systems are connected to your account yet — open Connections to add the ones you use."));
    }
  } catch (e) {}
}

/* ------------------------------------------------------------------ gate */
/* The session is an HttpOnly cookie set by /auth/login and /auth/signup; the
   page cannot read it and does not try. It asks /me on load and treats any
   later 401 as "the session ended". */

let signingUp = false;

function gateMode(signup) {
  signingUp = signup;
  $("gate-title").textContent = signup ? "Create an account" : "Sign in";
  $("gate-submit").textContent = signup ? "Create account" : "Sign in";
  $("gate-switch-text").textContent = signup ? "Already have an account?" : "No account yet?";
  $("gate-switch").textContent = signup ? "Sign in" : "Create one";
  $("gate-pass").autocomplete = signup ? "new-password" : "current-password";
  $("gate-email-wrap").hidden = !signup;
  $("gate-email").required = signup;
  $("gate-error").hidden = true;
}

/* Wipe what the last person saw. A shared screen must not hand the next
   account the previous one's conversation or balance. */
function resetSession() {
  state.history = [];
  state.payloads = [];
  state.me = null;
  state.conversationId = null;
  state.pins = [];
  closeDrawer();
  $("pins").innerHTML = "";
  $("convos").innerHTML = "";
  // The sign-in fields too. The next person must not find the last one's
  // username already in the box — typing after it produced "priyanikunj".
  $("gate-user").value = "";
  $("gate-pass").value = "";
  $("gate-email").value = "";
  $("thread").innerHTML = "";
  $("chips").innerHTML = "";
  $("unconfigured").innerHTML = "";
  $("results").innerHTML = "";
  $("config-status").innerHTML = "";
  $("empty").hidden = false;
  $("who").textContent = "";
  for (const node of document.querySelectorAll("[data-admin]")) node.hidden = true;
  updateSpend();
}

async function showGate() {
  if (!$("gate").hidden) return;      // several 401s can arrive at once
  resetSession();
  $("gate").hidden = false;
  // A fresh install has no accounts, so open on sign-up rather than a login
  // nobody can pass.
  try {
    const { accounts } = await (await fetch("/auth/status")).json();
    gateMode(accounts === 0);
    $("gate-lede").textContent = accounts === 0
      ? "No accounts exist yet. The first one you create is the admin."
      : "Ask across Zabbix, Zammad, Prometheus, Ceph and OpenSearch.";
  } catch (e) {
    gateMode(false);
  }
  $("gate-user").focus();
}

async function openApp() {
  $("gate").hidden = true;
  $("gate-pass").value = "";
  await refreshMe();
  checkConfigured();
  loadPins();
  show(location.hash.slice(1) || "chat");
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
      body: JSON.stringify({
        username: $("gate-user").value,
        password: $("gate-pass").value,
        email: signingUp ? $("gate-email").value : "",
      }),
    });
    const out = await res.json();
    if (out.ok) {
      openApp();
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

$("signout").addEventListener("click", async () => {
  try { await fetch("/auth/logout", { method: "POST" }); } catch (e) {}
  showGate();
});

// Already signed in from earlier (a live cookie)? Straight in; otherwise sign in.
(async () => {
  let res = null;
  try { res = await fetch("/me"); } catch (e) {}
  if (res && res.ok) openApp();
  else showGate();
})();
