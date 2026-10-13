"use strict";

/* The dashboard: each system's headline figures, drawn with ECharts.

   One request per system (/dashboard/<source>), so a tile fills in as soon as
   its own system answers and one that is down or not connected only blanks
   itself. Nothing here calls a model: the numbers come straight from each
   system's API, so opening or refreshing this page costs no tokens.

   Loaded before app.js and uses its helpers ($, el, api, show, ask) only at
   call time, by which point they exist. Text from the systems is always set
   with textContent, and chart tooltips use ECharts' rich-text mode, so a host
   or service name can never be read as markup. */

const Dashboard = (() => {
  const REFRESH_MS = 60000;

  // Each card names the estate its system watches. The systems do not overlap,
  // and a number read against the wrong one is this app's classic mistake.
  const CARDS = [
    { source: "zabbix", sys: "zabbix", name: "Zabbix", estate: "network devices and Windows hosts" },
    { source: "unticketed", sys: "mixed", name: "Zabbix + Zammad", estate: "active problems nobody has a ticket for" },
    { source: "zammad", sys: "zammad", name: "Zammad", estate: "helpdesk tickets" },
    { source: "prometheus", sys: "prometheus", name: "Prometheus", estate: "Linux servers and OpenStack VMs" },
    { source: "opensearch", sys: "opensearch", name: "OpenSearch", estate: "OpenStack service logs" },
    { source: "ceph", sys: "ceph", name: "Ceph", estate: "storage under OpenStack" },
    { source: "ceph_daemons", sys: "ceph", name: "Ceph daemons", estate: "the daemons' own container logs, not cluster health" },
  ];
  const STRIP = [
    { id: "hosts", sys: "zabbix", label: "Hosts down" },
    { id: "gaps", sys: "mixed", label: "Problems, no ticket" },
    { id: "tickets", sys: "zammad", label: "Open tickets" },
    { id: "targets", sys: "prometheus", label: "Targets up" },
    { id: "errors", sys: "opensearch", label: "Log errors today" },
    { id: "ceph", sys: "ceph", label: "Ceph health" },
  ];
  const STRIP_OF = { zabbix: "hosts", unticketed: "gaps", zammad: "tickets",
                     prometheus: "targets", opensearch: "errors", ceph: "ceph" };

  const charts = {};      // element id -> ECharts instance
  const last = {};        // source -> last response, so a theme change can redraw
  let timer = null, built = false, wired = false;

  const css = (name) =>
    getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const num = (n) => (n == null ? "–" : Number(n).toLocaleString("en-IN"));
  const clock = (iso) => new Date(iso).toLocaleTimeString("en-IN", { hour12: false });

  function askIQ(question) {
    show("chat");
    ask(question);
  }

  /* ------------------------------------------------------------ building */

  // A named child of `parent`, created on first use and reused after, so a
  // refresh updates a card in place rather than rebuilding it.
  function slot(parent, key, cls) {
    let node = parent.querySelector(`:scope > [data-key="${key}"]`);
    if (!node) {
      node = el("div", cls);
      node.dataset.key = key;
      parent.appendChild(node);
    }
    return node;
  }

  // A card is updated in place, so a part that no longer applies has to be
  // taken out by hand or it lingers beside the data that replaced it.
  function drop(parent, ...keys) {
    for (const key of keys) {
      const node = parent.querySelector(`:scope > [data-key="${key}"]`);
      if (node && charts[node.id]) { charts[node.id].dispose(); delete charts[node.id]; }
      if (node) node.remove();
    }
  }

  function askable(node, question) {
    if (!question || node.dataset.ask) return;
    node.dataset.ask = question;
    node.classList.add("dash-ask");
    node.tabIndex = 0;
    node.setAttribute("role", "button");
    node.title = "Ask IQCore: " + question;
    node.addEventListener("click", () => askIQ(question));
    node.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); askIQ(question); }
    });
  }

  function stats(parent, key, items) {
    const row = slot(parent, key, "dash-stats");
    row.innerHTML = "";
    for (const item of items) {
      const cell = el("div", "dash-stat" + (item.tone ? " " + item.tone : ""));
      cell.appendChild(el("b", null, item.value));
      cell.appendChild(el("span", null, item.label));
      askable(cell, item.ask);
      row.appendChild(cell);
    }
  }

  function list(parent, key, heading, rows, empty) {
    const box = slot(parent, key, "dash-list");
    box.innerHTML = "";
    box.appendChild(el("div", "card-title", heading));
    if (!rows.length) { box.appendChild(el("p", "dash-empty", empty)); return; }
    for (const row of rows) {
      const line = el("div", "dash-row");
      line.appendChild(el("span", "dash-row-main", row.main));
      if (row.tag) line.appendChild(el("span", "tag " + (row.tone || ""), row.tag));
      if (row.meta) line.appendChild(el("span", "dash-row-meta", row.meta));
      box.appendChild(line);
    }
  }

  function plot(parent, key, height, option, question) {
    const box = slot(parent, key, "dash-plot");
    box.style.height = height + "px";
    if (!box.id) box.id = "dash-" + Math.random().toString(36).slice(2);
    askable(box, question);
    const chart = charts[box.id] || (charts[box.id] = echarts.init(box));
    chart.setOption({ ...theme(), ...option }, true);
    return chart;
  }

  /* --------------------------------------------------------------- theme */
  /* Built from the page's own CSS variables on every draw, so the charts are
     the same warm monochrome as the rest of the app in either theme. */

  function theme() {
    return {
      color: [0, 1, 2, 3, 4, 5, 6, 7].map((i) => css("--s" + i)),
      textStyle: { fontFamily: css("--sans"), color: css("--muted") },
      animationDuration: 500,
      tooltip: {
        renderMode: "richText", confine: true,
        backgroundColor: css("--surface"), borderColor: css("--border"),
        textStyle: { color: css("--ink"), fontSize: 12 },
      },
    };
  }

  function axis(type, extra) {
    return {
      type,
      axisLine: { lineStyle: { color: css("--border-strong") } },
      axisTick: { show: false },
      axisLabel: { color: css("--muted"), fontSize: 11 },
      splitLine: { lineStyle: { color: css("--border"), type: "dashed" } },
      ...extra,
    };
  }

  const grid = (extra) => ({ left: 8, right: 16, top: 16, bottom: 8, containLabel: true, ...extra });
  const legend = () => ({ top: 0, right: 0, icon: "roundRect", itemWidth: 10, itemHeight: 10,
                          textStyle: { color: css("--muted"), fontSize: 11 } });
  const zoom = () => [{ type: "inside" }, {
    type: "slider", height: 18, bottom: 4, borderColor: css("--border"),
    fillerColor: css("--border-soft"), textStyle: { color: css("--muted"), fontSize: 10 } }];

  /* ----------------------------------------------------------- renderers */

  const render = {
    zabbix(body, d) {
      const h = d.hosts, p = d.problems;
      strip("hosts", num(h.down), h.down ? "bad" : "ok", "Which hosts are down or unreachable in Zabbix?");
      stats(body, "stats", [
        { label: "monitored hosts", value: num(h.total), ask: "How many hosts does Zabbix monitor?" },
        { label: "down or unreachable", value: num(h.down), tone: h.down ? "bad" : "",
          ask: "Which hosts are down or unreachable in Zabbix?" },
        { label: "active problems", value: num(p.total) + (p.capped ? "+" : ""),
          ask: "What are the active problems in Zabbix?" },
        { label: "not acknowledged", value: num(p.unacknowledged),
          ask: "Which Zabbix problems are not acknowledged?" },
      ]);
      const split = slot(body, "split", "dash-split");
      plot(split, "hosts", 220, {
        title: { text: num(h.up), subtext: "of " + num(h.total) + " up", left: "center", top: "36%",
                 textStyle: { color: css("--ink-strong"), fontSize: 26, fontFamily: css("--mono") },
                 subtextStyle: { color: css("--muted"), fontSize: 11 } },
        tooltip: { ...theme().tooltip, trigger: "item" },
        series: [{ type: "pie", radius: ["62%", "84%"], label: { show: false },
                   itemStyle: { borderColor: css("--surface"), borderWidth: 2 },
                   data: [
                     { name: "Up", value: h.up, itemStyle: { color: css("--hue-zabbix") } },
                     { name: "Down or unreachable", value: h.down, itemStyle: { color: css("--red-ink") } },
                     { name: "Unknown", value: h.unknown, itemStyle: { color: css("--faint") } },
                   ] }],
      }, "Show the availability of every Zabbix host");
      const tones = { Disaster: "--red-ink", High: "--hue-prometheus", Average: "--hue-zammad",
                      Warning: "--yellow-ink", Information: "--hue-ceph", "Not classified": "--faint" };
      const rows = [...p.by_severity].reverse();
      plot(split, "severity", 220, {
        grid: grid(),
        tooltip: { ...theme().tooltip, trigger: "axis", axisPointer: { type: "shadow" } },
        xAxis: axis("value", { minInterval: 1 }),
        yAxis: axis("category", { data: rows.map((r) => r.severity), splitLine: { show: false } }),
        series: [{ type: "bar", name: "Problems", barMaxWidth: 16,
                   itemStyle: { borderRadius: [0, 4, 4, 0] },
                   data: rows.map((r) => ({ value: r.count,
                     itemStyle: { color: css(tones[r.severity] || "--muted") } })) }],
      }, "Show the active Zabbix problems by severity");
    },

    unticketed(body, d) {
      strip("gaps", num(d.count), d.count ? "bad" : "ok", "Which active problems have no ticket?");
      stats(body, "stats", [
        { label: "active Zabbix problems with no Zammad ticket", value: num(d.count),
          tone: d.count ? "bad" : "", ask: "Which active problems have no ticket?" },
      ]);
      list(body, "top", "Most severe, oldest first", d.top.map((r) => ({
        main: r.host + " — " + r.problem, tag: r.severity,
        tone: /disaster|high/i.test(r.severity) ? "red" : "yellow",
        meta: Math.round(r.age_hours) + " h",
      })), "Every active problem has a ticket.");
    },

    zammad(body, d) {
      const many = "1,000+";
      const b = d.backlog;
      strip("tickets", b ? num(b.open) : many, "", "How many tickets are open?");
      stats(body, "stats", [
        { label: "new or open", value: b ? num(b.open) : many, ask: "How many tickets are open?" },
        { label: "unassigned", value: b ? num(b.unassigned) : "–", ask: "Which open tickets are unassigned?" },
        { label: "created today", value: d.created_today == null ? many : num(d.created_today),
          ask: "How many tickets were created today?" },
        { label: "closed today", value: d.closed_today == null ? many : num(d.closed_today),
          ask: "How many tickets were closed today?" },
      ]);
      if (!d.trend) {
        drop(body, "trend");
        slot(body, "note", "note").textContent =
          "More than 1,000 tickets in the last " + d.days + " days: too many to chart from search.";
        return;
      }
      drop(body, "note");
      plot(body, "trend", 240, {
        grid: grid({ top: 28, bottom: 30 }), legend: legend(),
        tooltip: { ...theme().tooltip, trigger: "axis" },
        xAxis: axis("category", { data: d.trend.map((r) => r.day.slice(5)), boundaryGap: false,
                                  splitLine: { show: false } }),
        yAxis: axis("value", { minInterval: 1 }),
        dataZoom: zoom(),
        series: [
          { name: "Created", type: "line", smooth: true, smoothMonotone: "x", symbolSize: 5, areaStyle: { opacity: 0.12 },
            color: css("--hue-zammad"), data: d.trend.map((r) => r.created) },
          { name: "Closed", type: "line", smooth: true, smoothMonotone: "x", symbolSize: 5,
            color: css("--hue-zabbix"), data: d.trend.map((r) => r.closed) },
        ],
      }, "How many tickets were created and closed each day over the last " + d.days + " days?");
    },

    prometheus(body, d) {
      const t = d.targets, v = d.vms;
      strip("targets", num(t.up) + "/" + num(t.total), t.up < t.total ? "bad" : "ok",
            "Which Prometheus scrape targets are down?");
      stats(body, "stats", [
        { label: "scrape targets healthy", value: num(t.up) + " / " + num(t.total),
          tone: t.up < t.total ? "bad" : "", ask: "Which Prometheus scrape targets are down?" },
        { label: "VMs running", value: num(v.running) + " / " + num(v.total), ask: "Which VMs are running?" },
      ]);
      // One set of rings per server: CPU, memory and the fullest disk.
      const rings = [["cpu", "CPU", "--s1"], ["memory", "Memory", "--s0"], ["disk", "Disk", "--s2"]];
      const step = 100 / (d.servers.length || 1);
      plot(body, "servers", 190, {
        title: d.servers.map((s, i) => ({ text: s.host, left: step * (i + 0.5) + "%", bottom: 0,
          textAlign: "center", textStyle: { color: css("--ink"), fontSize: 12, fontWeight: 600 } })),
        series: d.servers.map((s, i) => ({
          type: "gauge", center: [step * (i + 0.5) + "%", "44%"], radius: "78%",
          startAngle: 90, endAngle: -270, min: 0, max: 100,
          pointer: { show: false }, axisTick: { show: false }, splitLine: { show: false },
          axisLabel: { show: false }, title: { show: false },
          progress: { show: true, overlap: false, roundCap: true, clip: false },
          axisLine: { lineStyle: { width: 26, color: [[1, css("--track")]] } },
          data: rings.map(([key, label, tone], n) => ({
            value: s[key], name: label, itemStyle: { color: css(tone) },
            detail: { offsetCenter: [0, (n - 1) * 15], fontSize: 11, color: css(tone),
                      fontFamily: css("--mono"),
                      formatter: (value) => label + " " + (value == null ? "–" : Math.round(value) + "%") },
          })),
        })),
      }, "What is the CPU, memory and disk usage on the Linux servers?");
      plot(body, "cpu", 230, {
        grid: grid({ top: 28, bottom: 30 }), legend: legend(),
        tooltip: { ...theme().tooltip, trigger: "axis", valueFormatter: (x) => x + "%" },
        xAxis: axis("time", { splitLine: { show: false } }),
        // To the next ten above the busiest server, never below 50: a fixed 0-100
        // axis flattened three quiet servers into lines along the bottom.
        yAxis: axis("value", { min: 0, max: (v) => Math.min(100, Math.max(50, Math.ceil(v.max / 10) * 10 + 10)),
                               axisLabel: { color: css("--muted"), fontSize: 11, formatter: "{value}%" } }),
        dataZoom: zoom(),
        series: d.cpu_history.map((s) => ({ name: s.host, type: "line", smooth: true, smoothMonotone: "x",
                                            showSymbol: false, data: s.points })),
      }, "Show CPU usage on the Linux servers over the last 24 hours");
    },

    opensearch(body, d) {
      strip("errors", num(d.errors_today), "", "How many errors are in the OpenStack logs today?");
      stats(body, "stats", [
        { label: "log lines today", value: num(d.lines_today), ask: "How many OpenStack log lines were written today?" },
        { label: "errors today", value: num(d.errors_today), tone: d.errors_today ? "bad" : "",
          ask: "Show the errors in the OpenStack logs today" },
        { label: "warnings today", value: num(d.warnings_today), ask: "Show the warnings in the OpenStack logs today" },
      ]);
      const split = slot(body, "split", "dash-split");
      plot(split, "hourly", 230, {
        grid: grid(),
        tooltip: { ...theme().tooltip, trigger: "axis", axisPointer: { type: "shadow" } },
        xAxis: axis("time", { splitLine: { show: false } }),
        yAxis: axis("value", { minInterval: 1 }),
        series: [{ name: "Errors per hour", type: "bar", barMaxWidth: 12, color: css("--hue-opensearch"),
                   itemStyle: { borderRadius: [3, 3, 0, 0] }, data: d.errors_by_hour }],
      }, "Show OpenStack log errors by hour for the last 24 hours");
      const services = [...d.errors_by_service].reverse();
      plot(split, "services", 230, {
        grid: grid(),
        tooltip: { ...theme().tooltip, trigger: "axis", axisPointer: { type: "shadow" } },
        xAxis: axis("value", { minInterval: 1 }),
        yAxis: axis("category", { data: services.map((s) => s.service), splitLine: { show: false } }),
        series: [{ name: "Errors today", type: "bar", barMaxWidth: 14, color: css("--hue-opensearch"),
                   itemStyle: { borderRadius: [0, 4, 4, 0] }, data: services.map((s) => s.errors) }],
      }, "Which OpenStack services logged the most errors today?");
    },

    ceph(body, d) {
      const word = d.status.replace("HEALTH_", "");
      const tone = word === "OK" ? "ok" : word === "WARN" ? "warn" : "bad";
      strip("ceph", word, tone, "Is Ceph healthy? Show any failing health checks");
      stats(body, "stats", [
        { label: "cluster status", value: word, tone, ask: "Is Ceph healthy? Show any failing health checks" },
        { label: "OSDs up", value: num(d.osds.up) + " / " + num(d.osds.total),
          tone: d.osds.up < d.osds.total ? "bad" : "", ask: "Which Ceph OSDs are down?" },
        { label: "placement groups clean", value: d.pgs.clean_pct == null ? "–" : d.pgs.clean_pct + "%",
          ask: "What state are the Ceph placement groups in?" },
        { label: "raw TB used of " + num(d.raw.total_tb), value: num(d.raw.used_tb),
          ask: "How much Ceph storage is free, raw and usable?" },
      ]);
      list(body, "checks", "Failing health checks", d.checks.map((c) => ({
        main: c.message || c.type, tag: c.type, tone: "yellow" })), "None.");
      const split = slot(body, "split", "dash-split");
      plot(split, "raw", 210, {
        series: [{ type: "gauge", radius: "92%", center: ["50%", "60%"], startAngle: 200, endAngle: -20,
                   min: 0, max: 100, pointer: { show: false }, axisTick: { show: false },
                   splitLine: { show: false }, axisLabel: { show: false },
                   progress: { show: true, roundCap: true, width: 14, itemStyle: { color: css("--hue-ceph") } },
                   axisLine: { roundCap: true, lineStyle: { width: 14, color: [[1, css("--track")]] } },
                   title: { offsetCenter: [0, "32%"], color: css("--muted"), fontSize: 11 },
                   detail: { offsetCenter: [0, "-4%"], fontSize: 26, color: css("--ink-strong"),
                             fontFamily: css("--mono"), formatter: "{value}%" },
                   data: [{ value: d.raw.used_pct, name: "raw capacity used" }] }],
      }, "How much Ceph storage is free, raw and usable?");
      const hosts = {};
      for (const o of d.osd_layout) (hosts[o.host] = hosts[o.host] || []).push(o);
      plot(split, "osds", 210, {
        tooltip: { ...theme().tooltip, formatter: (p) => p.name + (p.value ? "  " + p.value + " GB" : "") },
        series: [{ type: "treemap", roam: false, nodeClick: false, breadcrumb: { show: false },
                   left: 0, right: 0, top: 0, bottom: 0,
                   label: { color: css("--tm-label"), fontSize: 10 },
                   upperLabel: { show: true, height: 18, color: css("--muted"), fontSize: 10 },
                   itemStyle: { borderColor: css("--surface"), borderWidth: 1, gapWidth: 1 },
                   levels: [{ itemStyle: { borderWidth: 0, gapWidth: 3 } },
                            { itemStyle: { borderColor: css("--surface-2"), borderWidth: 2, gapWidth: 1 } }],
                   data: Object.keys(hosts).sort().map((host) => ({
                     name: host, children: hosts[host].map((o) => ({
                       name: o.osd + (o.up ? "" : " (down)"), value: o.size_gb,
                       itemStyle: { color: css(o.up ? "--tm-up" : "--tm-down") } })) })) }],
      }, "Show the Ceph OSDs by host");
    },

    ceph_daemons(body, d) {
      stats(body, "stats", [
        { label: "distinct problems logged", value: num(d.distinct_problems),
          tone: d.distinct_problems ? "bad" : "", ask: "Is anything wrong with Ceph's daemons?" },
        { label: "daemons logging", value: num(d.daemons.length), ask: "Which Ceph daemons are logging?" },
        { label: "lines checked", value: num(d.lines_checked) },
      ]);
      if (!d.lines_checked && !d.daemons.length) {
        drop(body, "problems", "daemons");
        slot(body, "note", "note").textContent =
          "The daemon log index holds no lines right now. It keeps only a few hours, " +
          "so this means nothing was collected, not that nothing is wrong.";
        return;
      }
      drop(body, "note");
      list(body, "problems", "Most frequent problems", d.top_problems.map((r) => ({
        main: r.daemon + ": " + r.message, meta: "×" + num(r.occurrences) })), "None logged.");
      list(body, "daemons", "Last heard from", d.daemons.map((r) => ({
        main: r.daemon, meta: r.last_seen })), "No daemons are logging.");
    },
  };

  /* -------------------------------------------------------------- drawing */

  function strip(id, value, tone, question) {
    const cell = $("dash-strip-" + id);
    if (!cell) return;
    cell.querySelector("b").textContent = value;
    cell.className = "dash-kpi sys-" + cell.dataset.sys + (tone ? " " + tone : "");
    askable(cell, question);
  }

  function clear(body) {
    for (const node of body.querySelectorAll(".dash-plot")) {
      if (charts[node.id]) { charts[node.id].dispose(); delete charts[node.id]; }
    }
    body.innerHTML = "";
    delete body.dataset.state;
  }

  function draw(source) {
    const res = last[source], card = $("dash-card-" + source);
    if (!res || !card) return;
    const body = card.querySelector(".dash-body");
    card.querySelector(".dash-age").textContent =
      res.fetched_at ? "as of " + clock(res.fetched_at) : "";
    // A card that changes state (loading, ok, error) starts from empty.
    if (body.dataset.state !== res.status) { clear(body); body.dataset.state = res.status; }

    if (res.status === "ok") {
      try { render[source](body, res.data); }
      catch (e) { clear(body); body.appendChild(el("div", "note bad", "Could not draw this card: " + e.message)); }
      return;
    }
    body.innerHTML = "";
    if (STRIP_OF[source]) strip(STRIP_OF[source], "–", "");
    if (res.status === "not_connected") {
      const note = el("div", "note", "Not connected for your account. ");
      const link = el("a", null, "Add it on Connections");
      link.href = "#connections";
      note.appendChild(link);
      body.appendChild(note);
    } else {
      body.appendChild(el("div", "note bad", "Could not load: " + (res.message || "unknown error")));
    }
  }

  function build() {
    const stripBox = $("dash-strip"), gridBox = $("dash-grid");
    stripBox.innerHTML = gridBox.innerHTML = "";
    for (const k of STRIP) {
      const cell = el("div", "dash-kpi sys-" + k.sys);
      cell.id = "dash-strip-" + k.id;
      cell.dataset.sys = k.sys;
      cell.appendChild(el("span", "dash-kpi-label", k.label));
      cell.appendChild(el("b", null, "…"));
      stripBox.appendChild(cell);
    }
    for (const c of CARDS) {
      const card = el("section", "card dash-card sys-" + c.sys);
      card.id = "dash-card-" + c.source;
      const head = el("div", "card-head");
      const name = el("div", "dash-name");
      name.appendChild(el("span", "dash-dot"));
      name.appendChild(el("strong", null, c.name));
      name.appendChild(el("span", "dash-estate", c.estate));
      head.appendChild(name);
      head.appendChild(el("span", "dash-age"));
      card.appendChild(head);
      const body = el("div", "dash-body");
      body.dataset.state = "loading";
      body.appendChild(el("p", "dash-empty", "Loading…"));
      card.appendChild(body);
      gridBox.appendChild(card);
    }
    built = true;
  }

  async function load(fresh) {
    const button = $("dash-refresh");
    button.disabled = true;
    await Promise.all(CARDS.map(async (c) => {
      try {
        const res = await api("/dashboard/" + c.source + (fresh ? "?fresh=true" : ""));
        last[c.source] = res.ok ? await res.json()
          : { status: "error", message: "the server answered " + res.status };
      } catch (e) {
        if (e.message === "Signed out") return;
        last[c.source] = { status: "error", message: "the request failed" };
      }
      draw(c.source);
    }));
    button.disabled = false;
    $("dash-updated").textContent = "Checked " + clock(new Date().toISOString());
  }

  function redraw() {
    if (!built || $("page-dashboard").hidden) return;
    for (const c of CARDS) draw(c.source);
  }

  /* ----------------------------------------------------------------- api */

  function open() {
    if (!built) build();
    if (!wired) {
      $("dash-refresh").addEventListener("click", () => load(true));
      wired = true;
    }
    load(false);
    clearInterval(timer);
    timer = setInterval(() => {
      if (document.visibilityState === "visible") load(false);
    }, REFRESH_MS);
  }

  function close() {
    clearInterval(timer);
    timer = null;
  }

  // A shared screen must not show the next account the last one's numbers.
  function reset() {
    close();
    for (const id of Object.keys(charts)) { charts[id].dispose(); delete charts[id]; }
    for (const key of Object.keys(last)) delete last[key];
    if (built) { $("dash-strip").innerHTML = $("dash-grid").innerHTML = ""; }
    built = false;
  }

  window.addEventListener("resize", () => {
    for (const id of Object.keys(charts)) charts[id].resize();
  });
  // The charts take their colours from CSS variables at draw time, so a theme
  // change means drawing them again.
  new MutationObserver(redraw).observe(document.documentElement,
    { attributes: true, attributeFilter: ["data-theme"] });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", redraw);

  return { open, close, reset };
})();
