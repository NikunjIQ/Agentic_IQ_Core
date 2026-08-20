# Agentic IQ Core — Handover

Written to carry context into a fresh session. It covers what the app is, how it is
built, why it is built that way, what is wrong with it, and what was planned next.

Repo: `https://github.com/NikunjIQ/Agentic_IQ_Core` (private)
Local: `C:\Users\NikunjSinha(Identiqa\Desktop\Agentic_IQ_Core`
Scale: ~5,000 lines, 40 files.

---

## 1. What it is

A natural-language assistant over four IT systems, used by an operations team. You ask
in plain English; it routes to the right system, queries it, and answers conversationally.

Built for Identiqa Labs. The four systems watch **different, non-overlapping**
infrastructure — this is the single most important fact about the app, and blurring it
is the most common way to break it.

| System | Watches | Address |
|---|---|---|
| Ceph | Storage under OpenStack: pools, OSDs, RBD images | `192.168.1.30:8443` |
| Zabbix | Cisco routers/switches (`.73/.74/.75`), Windows hosts, a few Linux boxes | `192.168.1.205:8080` |
| Prometheus | Linux cluster `server01/02/03`, OpenStack, 7 VMs, containers | `192.168.1.29:9190` |
| OpenSearch | OpenStack service logs only (`flog-*`) | `192.168.1.32:9200` |
| Zammad | Helpdesk tickets | `192.168.1.205:8082` |

---

## 2. Architecture

```
Streamlit frontend  ->  FastAPI backend  ->  LangGraph  ->  6 tools  ->  4 systems
```

**LangGraph flow:** `classify` → (`clarify` | `simple_path` | `complex_path`) → `respond`

- **classify** — one 8B call. Returns complexity, chosen tool, and an ambiguity axis.
- **clarify** — asks the user a question. No model call, no tools; free.
- **simple_path** — extract args (8B) → run one tool → write the answer (70B).
- **complex_path** — orchestrator loop, up to 10 tool calls, then answers.
- **respond** — currently a pass-through placeholder.

### The governing principle

**The LLM decides what to look up and writes the final sentence. Code does everything
in between.**

This was arrived at the hard way. The original design asked the model to correlate
Zabbix problems with Zammad tickets by reading two tables in its context — but it only
ever saw `df.head(10)`, so with 48 problems it silently reported on 10 and sounded
equally confident. Joining, counting, filtering and ranking are now Python.

Corollary: **anything that must appear in an answer is stated as fact, not requested in
a prompt.** Three separate bugs in this project came from the model copying a prompt
example verbatim as a value.

### Key files

| File | Role |
|---|---|
| `backend/main.py` | FastAPI. `/chat`, `/chat/stream`, `/config`, `/config/test`, `/cache`, `/health` |
| `backend/agent/graph.py` | LangGraph wiring |
| `backend/agent/nodes/*.py` | classify, simple_path, complex_path, clarify_node, respond |
| `backend/agent/tool_registry.py` | The one place tools are registered |
| `backend/tools/clients.py` | Shared API clients for all four systems |
| `backend/tools/*_tool.py` | The four query tools |
| `backend/tools/zabbix/` | Zabbix branch implementations — client, common, metrics, hosts, items, monitoring, sla, audit. One tool, split by kind of question |
| `backend/tools/correlation.py` | Zabbix↔Zammad join |
| `backend/agent/config_store.py` | Credentials saved through the UI |
| `backend/agent/arg_guard.py` | Strips invented arguments |
| `backend/agent/coverage.py` | States what the answer could not see |
| `backend/agent/clarify.py` | Builds clarifying questions from live values |
| `backend/agent/voice.py` | Answer style |
| `backend/agent/suggestions.py` | Deterministic follow-up chips |
| `backend/agent/streaming.py` | SSE status + token streaming |
| `tests/run_evals.py` | Standalone eval harness (~98 checks) |

---

## 3. The correlation engine

The valuable part, and the reason the app exists.

**How the join works.** Zabbix creates tickets in Zammad via webhook and writes the
ticket id back onto the Zabbix event as tag `__zbx_zammad_ticket_id`. That is a real
foreign key. The engine:

1. Resolves the device (name, visible name, every interface IP → one identity)
2. `problem.get` with `selectTags`
3. Batch-resolves trigger ids → hosts (Zabbix returns a trigger, not a host)
4. Reads ticket ids off the tags
5. Fetches only those tickets
6. Joins in pandas; labels each row `EXACT` / `NO TICKET` / `DANGLING` with evidence

**Why exactness matters:** it can assert an *absence*. "This device is down and nobody
raised a ticket" is the finding ops actually want, and a model reading 10 rows can never
say it safely.

**A trap that was avoided:** Zabbix has a host named `Windows` and OpenStack has a VM
named `windows`. Different machines (`192.168.1.104` vs `10.0.0.41`). Name-based
matching would have confidently linked unrelated systems.

**Current state of the data:** 48 active problems, only 3 with tickets. Most rows
correctly return `NO TICKET`.

---

## 4. Infrastructure findings

Discovered by probing the live systems. Several are gaps on the infrastructure side
that no amount of app work can fix.

**Prometheus has zero alerting rules.** Alertmanager runs with a `default-receiver` and
no integration. So the entire Linux/OpenStack estate is monitored but unalerted, nothing
can fire, and no Prometheus→Zammad ticket can ever exist.

**Zabbix and Zammad run inside a VM this cluster hosts.** Both are at `192.168.1.205`,
which is the public IP of VM `jammy` on `server01`. If server01 degrades, the monitoring
and ticketing stack degrade with it — and Zabbix cannot see why, because it does not
monitor the hypervisor.

**Zabbix and Prometheus scrape disjoint targets** but watch the same machines from
different angles. Zabbix host "Nikunj VM" (`192.168.1.152`) is an OpenStack floating IP.
A Zabbix↔Prometheus correlation is possible via IP, and was designed but not built.

**OpenSearch holds no network-device data.** Searches for `FastEthernet`, `cisco`,
`192.168.1.73` all return zero. It is OpenStack service logs only.

**Log levels are inconsistently cased** — `error` appears 1,075,935 times, `ERROR`
23,657. Counting one spelling is wrong by 45×. Handled in code.

**Access logs carry no level field**, so level counts never sum to the total. The
summary says so explicitly.

**cAdvisor exposes no container names** — only opaque ids. Per-container questions
cannot be answered.

**No guest-OS label on VMs.** `os_type` is `hvm` for all seven. Windows-vs-Linux is
inferred from names and labelled as an inference.

**Neither Prometheus nor OpenSearch enforces authentication** — both answer identically
with and without credentials.

**Ceph's settings endpoint returns a credential in plaintext.** `GET /api/settings`
on the Ceph dashboard API includes `GRAFANA_API_PASSWORD`, and its value here is
`admin`. Any account that can log into the dashboard can read it. Two consequences:

1. *For this app* — this is a hard boundary, not a preference. Answering "what is the
   Grafana URL" by fetching that endpoint and handing the result to a model would put
   the password into the prompt and into the chat transcript, where it would persist.
   `CephClient.SAFE_SETTINGS` therefore **whitelists five setting names**
   (`GRAFANA_API_URL`, `GRAFANA_FRONTEND_API_URL`, `PROMETHEUS_API_HOST`,
   `ALERTMANAGER_API_HOST`, `GRAFANA_UPDATE_DASHBOARDS`) and returns nothing else. It
   is a whitelist rather than a filter on the way out, because a filter has to
   anticipate every key a future Ceph release might add. An eval asserts no
   `PASSWORD`/`SECRET`/`KEY`-shaped value ever reaches an answer.
2. *For the infrastructure* — the Grafana instance at `192.168.1.30:3000` also answers
   its API **without any authentication at all**: `/api/search` and `/api/datasources`
   both return real JSON to an unauthenticated caller. So the dashboards and the
   datasource configuration (hostnames, ports, usernames, database names) are readable
   by anyone who can reach the port, and the admin password is `admin`. This is an
   infrastructure problem no amount of app work fixes; it needs raising with whoever
   runs that host.

**Ceph's block-image endpoint pages silently.** `GET /api/block/image` returned 5
images of 37 by default and omitted the `volumes` pool entirely — the one holding the
Cinder data — so a caller trusting the default would report that Cinder volumes do not
exist. `limit=-1` is mandatory. `GET /api/pool` likewise reports every pool at zero
bytes without `stats=true`, which reads as an empty cluster.

**Ceph's API versions are per-endpoint and not guessable.** `/api/host` answers at
v1.3, `/api/block/image` and `/api/crush_rule` at v2.0, `/api/monitor` at v1.0 and
`/api/osd/settings` at v0.1. The wrong version returns **415**, which reads like a
malformed request rather than a header mismatch. `CEPH_API_VERSIONS` records the ones
that differ from the 1.0 default.

**Zabbix host inventory is switched off.** 10 of 11 hosts have `inventory_mode = disabled`;
the eleventh is automatic and holds only latitude and longitude. So EOL/EOS, model, serial
and OS version are *not* available from inventory, as the API docs would suggest. They come
from SNMP items instead — `system.sw.os[sysDescr.0]`, `system.hw.model`,
`system.hw.serialnumber`. Reading the docs alone would have produced a tool that returns
nothing.

**Zabbix holds no equipment lifecycle data at all.** No inventory field, no item. EOL/EOS
cannot be answered from monitoring; the tool says so and hands back model and serial for an
external lookup.

**Agent 1 vs Agent 2 cannot be distinguished.** That needs the `agent.variant` item, which
is collected on no host. `agent.version` exists on 4 hosts and reports a value on 1
(`7.4.12`); the rest are blank because those agents are not responding.

**Uptime lives under two different keys.** SNMP network kit reports
`system.net.uptime[sysUpTime.0]`, agent hosts report `system.uptime`. Searching one key
answers for half the estate.

**Services and SLAs are configured and working** — 6 services (5 leaf services tagged
`host: <ip>` under a root "Uptime" service), 2 SLAs both at a 90% SLO, daily and weekly.
`sla.getsli` returns real achieved figures. Note it returns **`sli = -1` for periods before
the SLA's effective date**: a "not measured" sentinel, not a percentage. Comparing it to the
target scores a service that has never been down as breaching.

**No maintenance windows are configured**, and no host is flagged in maintenance. Worth
knowing because a host in maintenance is expected to be unresponsive, and the availability
logic would currently report it as down with no ticket.

**Populated Zabbix families the app does not use yet**: `event` (1,203 — problem history,
against 51 current problems), `trend` (323k), `httptest` (3 web scenarios),
`drule`/`dhost`/`dservice` (network discovery), `action` (10), `mediatype` (40).
Empty, so not worth building for: `maintenance`, `correlation`, `userdirectory`, `iconmap`,
`connector`, `proxy`, `proxygroup`.

---

## 5. What was built, in order

| Commit | Work |
|---|---|
| `a119162` | Initial: Zabbix + Zammad, LangGraph, Streamlit |
| `91ac8c6` | Eval harness, conversation history, answer caching |
| `62c847c` | OpenSearch integration |
| `fbf1ea5` | Credentials through the Streamlit sidebar |
| `3b3b491` | Clarifying questions, argument guard, streaming |
| `0bd8d89` | Answers read as replies |
| `657bd2f` | Follow-up chips |
| `5be7d48` | Eval cache TTL (210s → 83s) |
| `3399225` | Say what the answer could not see |
| `9459c3b` | Frontend no longer needs secrets.toml |

Prometheus and the correlation engine were added in the earlier commits alongside the
Zabbix/Zammad work.

---

## 6. Bugs found and fixed — worth knowing, they show the failure modes

**The model invents argument values from prompt examples.** `date_filter='last 10 days'`
appeared on "How many tickets are in Open State?" because that phrase sat in the tool's
own example list. It turned 22 tickets into 2 while the answer still read 22 (taken from
the summary) — right answer, wrong table, no error. Fixed with `arg_guard.py`, which
strips time filters when the question mentions no time period.

**"Down" was a dead category.** Zabbix marks non-responding hosts `unreachable`, so
"how many devices are down" answered 0 while "which group has most down devices"
answered 2, from the same estate.

**Multi-value arguments were silently dropped.** `if/elif` chains meant "cpu & memory"
returned CPU only and "unreachable or unknown" returned only unreachable.

**Traffic queries returned discard counters.** Searching `net.if` also matches
`net.if.in.discards`. The model noticed and said so in its answer — the reasoning was
fine, the tool was bad.

**Audit lookups returned nothing** for every host: the code scanned the newest 100
entries and grepped by name, but the log is flooded by automated item edits and host
records are keyed by id.

**libvirt sums CPU across vCPUs**, so a busy 16-vCPU VM read as 598%.

**`re.escape` broke PromQL** — `\.` is not a valid escape inside a PromQL string, so
every host-filtered Prometheus query returned HTTP 400.

**Configuring via the sidebar blinded the test suite.** `apply_to_environment()` was
called only in `main.py`, so anything not importing main ran unconfigured. Now every
client ensures it.

**`st.secrets.get(key, default)` raises when no secrets.toml exists at all** — the
default only covers a missing key. A fresh clone crashed before rendering.

**Malformed tool calls killed whole requests.** Groq rejects them with
`tool_use_failed`; the orchestrator had no error handling around its LLM call.

---

## 7. Pros

- **Correlation is exact, not inferred.** Real foreign key, evidence per row, and it can
  assert absence.
- **Deterministic where it counts.** Joins, counts and filters are testable without a
  model and cost no tokens.
- **Honest about limits.** Tools state what they cannot answer rather than returning a
  misleading zero. Several answers explicitly say "this is inferred, not reported".
- **An eval harness that has repeatedly earned its keep** — caught a same-day bug, a
  credentials regression, and encodes every shipped bug as a permanent check.
- **Cheap conversational features.** Clarifying questions and follow-up chips add no
  model calls.
- **Configurable without touching files**, with connection testing before saving.
- **Fails legibly.** Rate limits and outages produce sentences, not 500s.

## 8. Cons

- **Token budget is the binding constraint.** ~100k/day on Groq free tier; a complex
  question costs ~13k. Roughly 7–11 complex questions a day.
- **Zammad fetching is wasteful.** Downloads 3,788 tickets to count 22 (~43s cold).
  Zammad offers no count-only search and has no Elasticsearch backing it, so this needs
  redesign.
- **No authentication anywhere.** `/chat` and `/config` are open to anyone who can reach
  the port — including the ability to overwrite credentials.
- **No logging at all.** No `import logging` in the codebase.
- **Credentials stored in cleartext** in `credentials.json` (gitignored, permissions
  best-effort).
- **`raw` payloads ship whole datasets to the browser** — every Zammad answer sends all
  3,788 tickets for a debug panel.
- **Hardcoded infrastructure IPs** as `os.getenv` fallbacks in six files, which turns
  missing config into a silent wrong-host connection.
- **Dependencies unpinned**; no Docker, no deployment config.
- **`prometheus_tool.py` is 600 lines** doing two jobs (host metrics and OpenStack/VMs).
- **Dead code**: `backend/models/schemas.py` is never imported; `ChatResponse` is defined
  twice and used nowhere; five client methods have no call sites; the `question`
  parameter on all four query tools is accepted and never read.
- **Eval suite hits live systems**, so it needs the real environment.

---

## 9. Planned work, discussed but not built

### Immediate

1. **Zammad server-side filtering.** Stop downloading the estate for questions the
   summary already answers; use search for row queries. Measure DB-backed search first —
   there is no Elasticsearch behind it.
2. **Cleanup pass.** Delete the dead code above; add logging; pin dependencies; add
   auth to the API; remove hardcoded IP fallbacks; cap `raw`.
3. **Make the table match the number.** An answer saying "22 open tickets" can currently
   attach a table of 2 rows, because the figure comes from the estate summary and the
   table from the filter. Label tables with what they contain and check row counts
   against stated figures.

### Correlation

4. **Zabbix ↔ Prometheus.** Join by IP: Zabbix host IPs against OpenStack floating and
   public-network IPs. Confirmed possible — "Nikunj VM" at `192.168.1.152` is a floating
   IP in the cluster. Would answer "is this VM slow, or is its hypervisor overloaded?".
   Blocker: the exporter does not expose which VM owns which floating IP; needs the
   OpenStack API directly, or MAC-based matching via Zabbix inventory.
5. **Zammad tags as a correlation dimension.** Tags already classify tickets
   (`component: storage` 80, `component: cpu` 83) and there are `host: <ip>` tags giving
   an exact device→ticket key better than body parsing. Tag search works. Adding
   `tag_filter` and `group_by: tag` would answer "tickets for storage/routers/OpenStack"
   generically.
6. **Prometheus ↔ Zammad** is impossible until Prometheus has alerting rules and an
   Alertmanager receiver pointing at Zammad.

### Security

7. **Guardrails discussed, not built:** an output scrubber redacting secret values and
   patterns from everything leaving the backend; deterministic refusal of credential
   probes; separating tool data from instructions to resist injection via ticket titles
   and log lines; sanitising exception text. Items 1, 2 and 4 there are deterministic and
   free.

### Larger ideas

8. **Unified entity resolution** across all four systems — one resolver instead of three
   separate identity maps.
9. **A canonical record shape** so correlation generalises to any pivot (entity, time,
   person, group) rather than pairwise joins.
10. **Provenance in the answer**, not just the debug panel.
11. **Trend awareness** — `PrometheusClient.over_time()` exists and is unused.
12. **Feedback capture** (thumbs up/down) to grow the eval set.

### Explicitly out of scope in the last brief

Question catalogue and tiered routing; token-reduction work; charts, forecasting,
anomaly detection, daily digests; any write or remediation capability.

---

## 10. Operational notes

**Credentials.** `.env` and `credentials.json` are both read; saved config wins. Neither
is in git. At handover, `.env` held Prometheus and OpenSearch while Zabbix, Zammad and
Groq came from the sidebar.

**Rotate the Prometheus and OpenSearch passwords** — they arrived via a Teams screenshot
and appear in the original chat transcript.

**Testing costs tokens.** Only the `--llm` tier does; offline and live-tool tiers are
free. The daily quota was exhausted twice by end-to-end testing.

**Restart the backend after changing `.env`** — `load_dotenv()` runs at import and
`--reload` only watches `.py` files.

**Windows note:** `pkill` does not exist; use `Stop-Process`. A stale uvicorn holding
port 8000 produces `WinError 10013`, which looks like a code failure and is not.

---

## 11. How to work on this codebase

1. **Probe before building.** Every good decision here came from querying the live system
   first. Several planned features turned out to be impossible, and several assumptions
   were wrong.
2. **Prefer code to prompting** for anything checkable. The model will copy your examples.
3. **Run the evals after every change** — `python tests/run_evals.py`.
4. **Watch the token budget.** Test the tool layer directly; it is free and where the
   logic lives.
5. **Keep the domain routing rules intact.** The four systems watch different machines,
   and blurring that produces confidently wrong answers.
6. **When a tool cannot answer, say so in the tool.** A misleading zero is worse than an
   error.
