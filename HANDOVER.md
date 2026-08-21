# Agentic IQ Core — Handover

Written to carry context into a fresh session. It covers what the app is, how it is
built, why it is built that way, what is wrong with it, and what was planned next.

Repo: `https://github.com/NikunjIQ/Agentic_IQ_Core` (private)
Local: `C:\Users\NikunjSinha(Identiqa\Desktop\Agentic_IQ_Core`
Scale: ~9,200 lines of Python, 61 tracked files.
Tests: 143 checks (`python tests/run_evals.py`), all passing.

---

## 1. What it is

A natural-language assistant over five IT systems, used by an operations team. You ask
in plain English; it routes to the right system, queries it, and answers conversationally.

Built for Identiqa Labs. The five systems watch **different, non-overlapping**
infrastructure — this is the single most important fact about the app, and blurring it
is the most common way to break it. Every confidently-wrong answer this project has
produced came from one system's number being attached to another system's question.

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
Streamlit frontend  ->  FastAPI backend  ->  LangGraph  ->  7 tools  ->  5 systems
```

**LangGraph flow:** `classify` → (`clarify` | `simple_path` | `complex_path`) → `respond`

- **classify** — one small-model call. Returns complexity, chosen tool, and an
  ambiguity axis. Sees one-line routing summaries, not the full tool docstrings.
- **clarify** — asks the user a question. No model call, no tools; free.
- **simple_path** — extract args (small, schema forced by `tool_choice`) → run one
  tool → write the answer (large).
- **complex_path** — orchestrator loop, up to 5 tool calls, then answers.
- **respond** — currently a pass-through placeholder.

**Models (Groq):** `openai/gpt-oss-20b` classifies and extracts, `openai/gpt-oss-120b`
reasons and writes. Both ids live in `backend/agent/models.py` and are overridable
with `IQCORE_REASONING_MODEL` / `IQCORE_FAST_MODEL`. The previous pair
(`llama-3.3-70b-versatile`, `llama-3.1-8b-instant`) was **retired by Groq mid-project**
and started 404-ing mid-answer — assume it will happen again.

**How a path is chosen.** The criterion is: can exactly ONE call to ONE tool answer
this? A question needing facts from two systems is complex even when it is one short
sentence. Unparseable classifier output fails safe to `complex`. `clarify_axis`
outranks complexity in `route_by_complexity`, so a question naming nothing routable
gets a question back regardless of what the model decided.

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
| `backend/tools/*_tool.py` | The five query tools (zabbix, zammad, prometheus, opensearch, ceph) |
| `backend/agent/models.py` | The two Groq model ids, in one place |
| `frontend/live_graph.py` | Draws the graph as it executes |
| `frontend/ceph_treemap.py` | Draws the CRUSH hierarchy as a treemap |
| `backend/tools/zabbix/` | Zabbix branch implementations — client, common, metrics, hosts, items, monitoring, sla, audit. One tool, split by kind of question |
| `backend/tools/correlation.py` | Zabbix↔Zammad join |
| `backend/agent/config_store.py` | Credentials saved through the UI |
| `backend/agent/arg_guard.py` | Strips invented arguments |
| `backend/agent/coverage.py` | States what the answer could not see |
| `backend/agent/clarify.py` | Builds clarifying questions from live values |
| `backend/agent/voice.py` | Answer style |
| `backend/agent/suggestions.py` | Deterministic follow-up chips |
| `backend/agent/streaming.py` | SSE status, node events, token streaming |
| `backend/agent/telemetry.py` | Per-call token usage, from Groq's own figures |
| `tests/run_evals.py` | Standalone eval harness (143 checks) |

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

### This session (most recent last)

| Commit | Work |
|---|---|
| `2ec0d5b` | Zabbix widened beyond its original 24 sample questions: metric vocabulary 4 → 9 families, new SLA branch, module split into `tools/zabbix/` |
| `8292e6f` | Zabbix username/password auth fixed for 6.4+ (was broken twice over, invisibly) |
| `3d8dec2` | Metric questions about a CLASS of device (`group_filter` on `items`) |
| `e73cebf` | Groq retired both models; replaced with the gpt-oss pair, ids centralised |
| `0a04e72` | Token cut: routing summaries for classify, leaner schemas, `MAX_TOOL_CALLS` 10 → 5 |
| `d846153` | Zammad widened using the 6.5.2 API: tag filtering, text search, SLA definitions, time accounting, named queues, ticket conversation |
| `eff6d01` | Live graph — watch nodes execute in the frontend while a question runs |
| `369b96f` | **Ceph added as a fifth system** |
| `c90d22a` | Removed 726 KB of probe output committed by mistake |
| `9ba5a1c` | Fixed a confidently wrong cross-system answer (see §6) |
| `eb44481` | The 19 official Ceph questions, all answerable |
| `b1ad5a2` | CRUSH treemap in the frontend; settings boundary recorded |
| `7954bca` | Ceph added to the credential sidebar, with a connection test |
| `a44d07a` | **Token telemetry** — every answer shows where its tokens went |

**The method that produced all of it**, and the one to keep using: read the API
reference for the *running version* to get the candidate surface, then **probe the live
system** to find what is actually populated, and build only for the overlap. The docs
and reality disagreed on the highest-value item every single time. Zabbix inventory was
switched off; Ceph's RGW was not deployed; Zammad's ticket links were unused. Building
from documentation alone would have produced correct, well-tested functions that return
nothing.

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

### Found this session

**A dead parameter swallowed the question's subject.** Every query tool accepted a
`question` argument and **no tool body read it** — output was byte-identical with and
without. Asked *"is there a ticket open for the ceph network alert?"*, the extractor put
`'ceph network alert'` into `question`, where it vanished; the tool filtered by state
alone, and the answer married the estate's open-ticket count (22) to a subject it had
never filtered on. Removed from all five tools. **The lesson: an unread parameter is
worse than no parameter, because it makes a call look scoped when it is not.**

**Cross-system questions classified as simple.** The classifier's complex examples were
all long sentences, so a short two-system question read as one-system. It now carries a
decisive test — *does answering require facts from TWO systems?* — with worked examples.

**Zabbix username/password auth was broken twice over, invisibly.** `user.login`'s
`user` parameter became `username` in 6.0 and was removed in 6.4; 6.4 also dropped the
request-body `auth` field in favour of the `Authorization` header. Both were wrong. It
was never noticed because the app authenticates with a token, so the fallback was dead
code. Anyone configuring username/password through the sidebar would have hit it.

**Zabbix `sla.getsli` returns `sli = -1` for periods before the SLA's effective date.**
A sentinel, not a percentage. Compared against the 90% target it reported a service with
zero downtime as breaching in 2 of 7 periods.

**Metric questions about a class of device were unanswerable.** `items` required a
single `host_filter`, so *"what is the uptime of the routers"* routed to
`query_type='hosts'` — which reports up/down and **cannot report a metric at all** — and
answered "2 routers, both up, go and check their uptime in Zabbix". `group_filter` now
works on `items`, and a `host_filter` that matches no host falls back to a group lookup.

**Zammad's search caps at 200 rows** whatever `limit` asks for — 200, 500, 1000 and 2000
all return 200. A tag with more tickets read as exactly 200. Now reported as a MINIMUM
with the authoritative count from the tag index beside it.

**Ceph's `/api/block/image` pages silently** — 5 images of 37, omitting the whole
`volumes` pool holding 543 GB of Cinder data. **`/api/pool` reports every pool at zero
bytes without `stats=true`.** Both would have produced confident, wrong answers.

**Compressing tool descriptions broke extraction.** Shortening the Prometheus metric
shortcut list stopped `'networks'` and `'vms'` being recognised: those descriptions are
**matching hooks for the model, not documentation**. The prose around them is
compressible; the enumerations are not. Only the `--llm` eval tier caught it.

**`clarify.py` did not know the word "ceph"** or any storage vocabulary, so *"is ceph
healthy?"* was treated as too vague to route and answered with a clarifying question
that did not even list Ceph.

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

- **Two token caps apply, not one.** The gpt-oss models allow **8,000 tokens/minute**,
  so three questions in quick succession will 429 — that is the one that bites during
  demos. But there is **also a 200,000 tokens/day ceiling on `gpt-oss-20b`**, and this
  document previously said the daily budget had gone away. It has not. Two full runs of
  `tests/score_routing.py` (~110,000 tokens each) exhausted it, after which nothing in
  the app or the test suite could call a model until it reset. The error names which
  cap was hit — read it, because the two need completely different responses: wait a
  minute, or wait until tomorrow.
- **The `--offline` eval tier is not actually free.** It advertises "no network, ~13s",
  but `test_every_graph_node_announces_itself` invokes the compiled graph, which calls
  models. It contributed to spending the daily budget above, and once that budget is
  gone this test fails in a way that looks like a code regression.
- **The costs are now measured, and the old estimates were wrong.** `agent/telemetry.py`
  records Groq's own usage per call and every answer displays it. Measured on a simple
  single-tool question:

  | Operation | Model | In | Out | Thinking | Total |
  |---|---|---:|---:|---:|---:|
  | classify | gpt-oss-20b | 1,034 | 136 | 110 | 1,170 |
  | extract | gpt-oss-20b | 1,056 | 78 | 52 | 1,134 |
  | synthesise | gpt-oss-120b | 647 | 256 | 107 | 903 |
  | **total** | | | | **269** | **3,207** |

  That is **~40% of a minute's allowance for the cheapest kind of question**, against
  the ~1,528 that character-counting predicted. Any future optimisation should start
  from telemetry, not from estimates.
- **Classification is the single most expensive operation** at ~36% — more than writing
  the answer. It was optimised once already (routing summaries cut it from 3,257 to 726
  estimated tokens) and is still the largest line. Its input is ~1,034 tokens measured,
  so the estimate understated it too.
- **Reasoning is ~57% of all output tokens.** gpt-oss models think before answering;
  that work is billed and invisible in the reply. It is broken out separately in the
  telemetry because it is a real lever nobody could see before.
- **Complex questions are still expensive.** 76% of each orchestrator iteration is tool
  schemas, resent every loop. Trimming got them from 3,804 to ~2,900 tokens; going
  further needs schema subsetting, which is designed but not built (see §9).
- **Zammad still downloads the whole estate** (3,825 tickets, ~43s cold) for most
  questions. `tag_filter` and `search` are now resolved server-side and skip it, but the
  other filters do not.
- **No authentication anywhere.** `/chat` and `/config` are open to anyone who can reach
  the port — including the ability to overwrite credentials.
- **No logging at all.** No `import logging` in the codebase.
- **Credentials stored in cleartext** in `credentials.json` (gitignored, permissions
  best-effort).
- **`raw` payloads ship whole datasets to the browser.**
- **Hardcoded infrastructure IPs** as `os.getenv` fallbacks, which turns missing config
  into a silent wrong-host connection.
- **Dependencies unpinned**; no Docker, no deployment config.
- **Two tools are getting large**: `prometheus_tool.py` at 589 lines doing two jobs
  (host metrics and OpenStack/VMs), and `ceph_tool.py` at 568 lines with 12 query types.
  `zabbix_tool.py` hit 1,001 before it became `tools/zabbix/`; these are next.
- **Dead code**: `backend/models/schemas.py` is never imported; `ChatResponse` is defined
  twice and used nowhere; several client methods have no call sites. (The `question`
  parameter is now gone — see §6.)
- **Ceph auth is the one non-GET in the app.** The dashboard issues a short-lived JWT and
  offers no long-lived API key, so `CephClient` performs exactly one POST — the login —
  caching the token until its own `exp`. There are deliberately no create/update/delete
  methods anywhere in that client.
- **Eval suite hits live systems**, so it needs the real environment.

---

## 9. Planned work

Ordered so a fresh session can pick one up without re-deriving the context.

### Ready to build — the design is settled

1. **The three-way VM join: Ceph ↔ Prometheus ↔ OpenSearch.** *Probed and confirmed, not
   built.* OpenStack names Ceph RBD images after the instance UUID
   (`vms/<uuid>_disk`), Prometheus carries the same UUID as `instance_id` on
   `libvirt_domain_openstack_info`, and nova writes `[instance: <uuid>]` into its logs.
   One identifier, three systems — the same class of key as the Zabbix↔Zammad tag.
   `query_ceph` already returns `openstack_uuid` on every image row.
   **Two things make or break it.** Search the logs for `"instance: <uuid>"`, NOT the
   bare UUID: the bare form matches ~569,000 routine polling lines per VM (one neutron
   request lists every VM at once), while the structured marker gives 20–965 — a 4,800×
   noise reduction. And default to warning/error, which cuts those to 4–953. Get either
   wrong and it returns half a million rows of nothing that a model will summarise as
   "high activity".
   Worth knowing: only 1 of 7 VMs (`jammy`) is Ceph-backed; the rest are on local disk.

2. **Schema subsetting in the orchestrator** — item 5 of the token work, deliberately
   deferred. Measured options: *progressive detail* (iteration 1 binds everything, later
   iterations keep full schemas only for tools already used and drop the rest to
   name + one-liner + parameter names) gives ~25%, rising to ~30% with a modest trim of
   the 950-token system prompt. Nothing becomes uncallable, which hard subsetting risks.
   **Measure first**: instrument the loop to record iterations per question — the "4 tool
   calls" figure everything is costed against has never been verified.

3. **Split the two large tools.** `prometheus_tool.py` (589 lines, two jobs) and
   `ceph_tool.py` (568 lines, 12 query types) into packages, exactly as `zabbix_tool.py`
   became `tools/zabbix/`. Tool surface unchanged, so the free evals fully verify it.

4. **Cross-system cases in the `--llm` tier.** The confidently-wrong Ceph/Zammad answer
   was found by the user, not by a test. The classification fix is only proven on that
   one question.

### Blocked or impossible

5. **Zabbix ↔ Prometheus by IP.** Confirmed possible in principle — "Nikunj VM"
   (`192.168.1.152`) is an OpenStack floating IP. Blocker: the exporter does not expose
   which VM owns which floating IP; needs the OpenStack API directly or MAC matching.
6. **Prometheus ↔ Zammad** is impossible until Prometheus has alerting rules and an
   Alertmanager receiver pointing at Zammad. Neither exists.
7. **Ceph ↔ Zammad** has no join key at all. Only Zabbix raises tickets, via webhook.
   `query_zammad` now says so when a text search for a Ceph or Prometheus alert finds
   nothing — that absence is a genuine finding, not a search failure.

### Housekeeping, unchanged and still true

8. **Cleanup pass.** Delete the remaining dead code; add logging; pin dependencies; add
   auth to the API; remove hardcoded IP fallbacks; cap `raw`.
9. **Make the table match the number.** An answer saying "22 open tickets" can attach a
   2-row table, because the figure comes from the estate summary and the table from the
   filter.
10. **Security guardrails discussed, not built:** an output scrubber; deterministic
    refusal of credential probes; separating tool data from instructions to resist
    injection via ticket titles and log lines; sanitising exception text.

### Larger ideas

11. **Unified entity resolution** across all five systems — one resolver instead of
    several identity maps. `correlation.resolve_device` is the closest thing and is
    already reused by the Zabbix SLA branch.
12. **A canonical record shape** so correlation generalises to any pivot rather than
    pairwise joins.
13. **Provenance in the answer**, not just the debug panel.
14. **Trend awareness** — `PrometheusClient.over_time()` exists and is unused; Ceph and
    Zabbix both hold trend data (323k Zabbix trend rows).
15. **Feedback capture** (thumbs up/down) to grow the eval set.

---

## 10. Operational notes

**Credentials.** `.env` and `credentials.json` are both read; saved config wins. Neither
is in git, so **a fresh clone or a `git pull` on another machine has no credentials** —
that is the first thing to hit on a new host. All five systems plus Groq can be entered
through the Streamlit sidebar, which writes `credentials.json` and can **Test** each
connection before saving. That is usually faster than copying `.env` across, and the
test tells you whether that machine can actually reach each system.

**Ceph specifics.** `CEPH_URL` must be the API base (`https://192.168.1.30:8443`), not
the dashboard's browser address — the `/#/api-docs` fragment the UI shows is a frontend
route the API never sees. The connection test strips it anyway, but the tool is happier
with the base. `CEPH_VERIFY_TLS=false` because the dashboard uses a self-signed
certificate.

**Rotate the Prometheus and OpenSearch passwords** — they arrived via a Teams screenshot
and appear in the original chat transcript.

**Raise the Grafana exposure** with whoever runs `192.168.1.30`: its API answers
unauthenticated on port 3000, and Ceph's `/api/settings` returns the Grafana password in
plaintext (it is a default value). See §4.

**Several credentials are dictionary words** — the system's own name, or `admin`. That
surfaced while scanning the repo for leaked secrets: no secret is committed, but the
scan produced false positives precisely because the values are ordinary words.

**Testing costs tokens.** Only the `--llm` tier does; offline and live-tool tiers are
free. That tier is paced at 8s between cases (`IQCORE_EVAL_PACING`) because the models
are capped per minute — at the old 1s it rate-limits and the failure looks like an
extraction regression.

**Restart the backend after changing `.env`** — `load_dotenv()` runs at import and
`--reload` only watches `.py` files.

**Windows note:** `pkill` does not exist; use `Stop-Process`. A stale uvicorn holding
port 8000 produces `WinError 10013`, which looks like a code failure and is not. There
is no venv in the repo on Windows; on Linux create one with `python3 -m venv venv &&
source venv/bin/activate` (note `bin`, not `Scripts` — the README's install block is
Linux-flavoured).

**Running it.** `.claude/launch.json` starts the frontend; the backend is
`cd backend && uvicorn main:app --host 0.0.0.0 --port 8000`.

---

## 11. How to work on this codebase

1. **Probe before building.** Read the API reference for the *running version* to get
   the candidate surface, then query the live system to find what is actually populated,
   and build only for the overlap. The docs and reality disagreed on the highest-value
   item every single time: Zabbix inventory switched off, Ceph RGW not deployed, Zammad
   ticket links unused. Building from documentation alone produces correct functions
   that return nothing.
2. **Prefer code to prompting** for anything checkable. The model will copy your
   examples verbatim as values.
3. **Run the evals after every change** — `python tests/run_evals.py` (143 checks, ~6
   min, free). Use `--offline` (~13s) while iterating.
4. **Watch the per-minute budget.** Test the tool layer directly; it is free and where
   the logic lives. Three complex questions back to back will 429.
5. **Keep the domain routing rules intact.** The five systems watch different machines,
   and blurring that produces confidently wrong answers. Ceph is storage *under*
   OpenStack; Prometheus watches VM CPU and memory *above* it; "how many VMs" is a
   Prometheus question, not a Ceph one.
6. **When a tool cannot answer, say so in the tool.** A misleading zero is worse than an
   error. Several answers now state an absence as a finding — "no ticket was raised",
   "no disk failures are recorded", "RGW is not deployed".
7. **Suspect silent truncation in every new API.** Three systems here paginate or cap
   without saying so: Zammad search stops at 200, Ceph's image listing returned 5 of 37,
   Zabbix item searches match template items that never hold values. Always check a
   count against a second source before trusting it.
8. **When a sample question maps to a convenient stand-in, the eval is lying.** The
   Zabbix corpus passed while "what is the uptime of routers" returned nothing, because
   the mapping pointed at a single-device query. Map the question as asked.
9. **Measure before optimising, and never estimate tokens.** Character counting
   understated a simple question by roughly half and got the ranking of operations
   wrong. `agent/telemetry.py` reports Groq's own figures per call; read those.
10. **Instrumentation must never cost an answer.** Merging stream chunks to recover
    usage broke a stream whose chunks could not be added. `stream_text` now degrades to
    unmeasured rather than raising; `stream_with_tools` deliberately does not, because
    there the merge is load-bearing — `tool_calls` are only recoverable from the merged
    message.
