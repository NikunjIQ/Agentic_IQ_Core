# Agentic IQ Core — Handover

Written to carry context into a fresh session. It covers what the app is, how it is
built, why it is built that way, what is wrong with it, and what was planned next.

Repo: `https://github.com/NikunjIQ/Agentic_IQ_Core` (private)
Local: `C:\Users\HP\Desktop\Agentic_IQ_Core-main` (also `C:\Users\NikunjSinha(Identiqa\Desktop\Agentic_IQ_Core` on the work machine)
Scale: ~12,100 lines of Python across 52 files, plus ~2,500 lines of static UI.
Tests: pytest. `pytest -m "not live and not llm"` is the free offline tier (122 tests,
~13s). Plain `pytest` also runs 74 `live` tests against the real estate and takes
many minutes. See §Tests in README.md.

---

## 0. Current state — read first

**Last commit on main:** `821244d` (static page served by FastAPI, login gate).

**Built and verified, NOT yet committed** (waiting for the user's go-ahead):

1. **SQLite** replaced the JSON files: users, sessions, credit accounts, usage, per-user
   encrypted service credentials, plus `turns` and `favourites`. See `agent/db.py`.
2. **Per-user credentials.** Each user connects their own systems; a question about an
   unconnected system says so instead of borrowing someone else's. Enforced by a
   ContextVar (`config_store.use_user`), per-owner client caches and a user-keyed answer cache.
3. **Charts in answers** — bar (with "Chart by"), line (hover crosshair), treemap.
4. **Prometheus history** — `query_prometheus(history_minutes=…)` does a range query and
   returns long-format `{time, series, host, value, unit}` rows. `arg_guard` strips it
   unless the question mentions a period.
5. **Dark mode** — CSS variables; SVG drawn with classes, not hard-coded colours.
6. **Saved conversations + pinned questions** — History drawer; six endpoints, every one
   scoped to the signed-in user (another user's id is a 404).

Accounts in the local DB: `nikunj` (admin, a test account), `Nikunj231_` (the user's own,
regular), `prachiyash`, `priya` (test). Promoting `Nikunj231_` and deleting the test
accounts was offered and not yet answered.

**Working rules the user has set:**

- **Commit/push only when the user explicitly says so**, and **directly on `main`** — never a branch.
- **Never commit** `credentials.json`, `authentication.json`, `iqcore.db*`, `secret.key`
  (all gitignored).
- Files are **LF**. On Windows, write with `write_bytes(text.encode())`, not `write_text`,
  or the whole file shows as changed.
- The user likes lean code (the "ponytail" style): reuse what exists, shortest working
  diff, no speculative abstractions.

---

## 1. What it is

A natural-language assistant over six data sources, used by an operations team. You
ask in plain English; it routes to the right system, queries it, and answers
conversationally.

Built for Identiqa Labs. The systems watch **different, non-overlapping**
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
| OpenSearch #2 | Ceph's **container** stdout (`podman-*`) — see §7 | `192.168.1.152:9200` (HTTPS) |

---

## 2. Architecture

```
Browser page  ->  FastAPI (serves both)  ->  LangGraph  ->  9 tools  ->  6 sources
```

**LangGraph flow:** `classify` → (`clarify` | `simple_path` | `complex_path`) → `respond`

- **classify** — one small-model call. Returns complexity, chosen tool, and an
  ambiguity axis, through a **forced tool-call schema** (not free-text JSON). Sees
  one-line routing summaries, not the full tool docstrings.
- **clarify** — asks the user a question. No model call, no tools; free. Ends the run.
- **simple_path** — extract args (small model, schema forced by `tool_choice`) → run
  one tool → write the answer (large model).
- **complex_path** — reactive orchestrator loop, up to 5 tool calls, then answers.
- **respond** — still a pass-through placeholder.

**Models (Groq):** `openai/gpt-oss-20b` classifies and extracts, `openai/gpt-oss-120b`
reasons and writes. Both ids live in `backend/agent/models.py` and are overridable with
`IQCORE_REASONING_MODEL` / `IQCORE_FAST_MODEL`. The previous pair
(`llama-3.3-70b-versatile`, `llama-3.1-8b-instant`) was **retired by Groq mid-project**
and started 404-ing mid-answer — assume it will happen again.

**How a path is chosen.** The criterion: can exactly ONE call to ONE tool answer this?
A question needing facts from two systems is complex even when it is one short
sentence. `clarify_axis` outranks complexity in `route_by_complexity`.

### The governing principle

**The LLM decides what to look up and writes the final sentence. Code does everything
in between.**

This was arrived at the hard way. The original design asked the model to correlate
Zabbix problems with Zammad tickets by reading two tables in its context — but it only
ever saw `df.head(10)`, so with 48 problems it silently reported on 10 and sounded
equally confident. Joining, counting, filtering and ranking are now Python.

Corollary: **anything that must appear in an answer is stated as fact, not requested in
a prompt.** Three separate bugs came from the model copying a prompt example verbatim
as a value.

---

## 3. What every file does

### `backend/` — entry point

| File | Lines | Role |
|---|---:|---|
| `main.py` | 460 | FastAPI. Serves the page at `/` (asset links stamped with mtime, `no-store`) and the API: `/auth/{status,login,signup,logout}`, `/me`, `/config`, `/config/test`, `/admin/credits`, `/chat/stream`, `/conversations[/{id}]`, `/favourites[/{id}]`, `/health`. `current_user` / `admin_user` dependencies read the session cookie. `/chat/stream` charges usage and saves the turn **before** the `done` event, so the meter never lags. **`/config` and `/config/test` are deliberately sync `def`, not `async`** — see §8. |

### `backend/agent/` — orchestration

| File | Lines | Role |
|---|---:|---|
| `graph.py` | 44 | LangGraph wiring. `route_by_complexity` checks `clarify_axis` first. |
| `state.py` | 16 | `AgentState` TypedDict. `complexity` is `Literal["simple","complex"]`. |
| `models.py` | 28 | The two Groq model ids, in one place, env-overridable. |
| `vocabulary.py` | 289 | **Single source of routing vocabulary.** `SURFACE` maps each tool's capabilities to the words people use; `COLLISIONS` records nouns two systems share, with the default this estate means. Documents, never decides — see §5. |
| `tool_registry.py` | 104 | The one place tools are registered. `ROUTING_SUMMARIES` is what classify sees. |
| `tool_schemas.py` | 14 | Builds JSON schemas from the tool objects. |
| `clarify.py` | 166 | Builds clarifying questions from live values. `looks_ambiguous()` is a floor under the model; `resolves_to_one_tool()` is a ceiling over it. `_ANCHOR` derives from `vocabulary.py`. |
| `metadata.py` | 121 | Fetches live host groups / ticket states / Prometheus nodes and injects them into extraction prompts so the model uses real values. 5-min cache. |
| `coverage.py` | 115 | States what an answer could not see — truncated tables, empty results that mean "not collected here". |
| `arg_guard.py` | 75 | Strips filters the question never asked for. |
| `answer_cache.py` | 150 | 60s TTL, keyed on user + question + history — one account's answer is never served to another. Never caches errors. |
| `telemetry.py` | 149 | Per-call token usage from Groq's own figures. Never estimates. |
| `streaming.py` | 162 | SSE events: `status`, `node`, `tool`, `token`. Degrades to unmeasured rather than raising. |
| `history.py` | 48 | Recent conversation turns, truncated. |
| `voice.py` | 65 | Answer style. |
| `suggestions.py` | 98 | Deterministic follow-up chips. No model call. |
| `config_store.py` | 318 | Per-request settings. `get()` answers from the signed-in user's own credentials; `require()` raises `NotConnected` for a system they haven't connected. The Groq key stays a system setting. |
| `db.py` | 473 | SQLite (stdlib `sqlite3`, WAL, foreign keys on, one connection per operation). Tables: `users`, `sessions` (only the cookie's sha256), `credit_accounts`, `usage` (one row per answer, integer paise), `service_credentials` (Fernet-encrypted), `turns` (saved Q&A + trimmed payload), `favourites` (unique per user+question); view `credit_balance` computes spent/remaining. Schema is `IF NOT EXISTS`, so new tables appear on next start. Imports the old JSON files once, into an empty database. |
| `auth_store.py` | 75 | Sign-up and password checks (salted PBKDF2). The first account is the admin. |
| `config_test.py` | 266 | Connection tests for each system. Deliberately does **not** retry — see §6. |

### `backend/agent/nodes/`

| File | Lines | Role |
|---|---:|---|
| `classify.py` | 253 | Routing. Forced schema with the tool names as an enum, retry-escalation to the larger model, floor/ceiling reconciliation. The most expensive single call in the app. |
| `complex_path.py` | 297 | Reactive orchestrator loop. `MAX_TOOL_CALLS=5`, `LOOP_SUMMARY_CHARS=1200`, `LOOP_PREVIEW_CHARS=500`. Its system prompt carries RULE -1 through RULE 4, which encode the domain boundaries. |
| `simple_path.py` | 147 | Extract → one tool → synthesise. Escalates to the larger model if extraction is malformed. |
| `clarify_node.py` | 32 | Returns the clarifying question. Ends the run. |
| `respond.py` | 14 | Pass-through placeholder. |

### `backend/tools/` — the query layer

| File | Lines | Role |
|---|---:|---|
| `clients.py` | 898 | Shared API clients for every system, plus `request()` — the retry helper all HTTP goes through. Credentials read through `cfg()`; `Cached` keeps one slot per user; `PrometheusClient.over_time()` does range queries. |
| `ceph_tool.py` | 677 | Ceph cluster: health, capacity (raw *and* usable), pools, OSDs, hosts, RBD images, snapshots, CephFS, CRUSH, cluster logs, monitoring endpoints. 12 query types. |
| `prometheus_tool.py` | 650 | Host metrics and OpenStack/VMs. `_vm_inventory()` resolves libvirt domains to OpenStack identity. `history_minutes` switches a host metric to a range query; `_series_rows()` returns long-format rows and the summary carries latest/min/max per series. |
| `zammad_tool.py` | 524 | Tickets, states, agents, SLA, tags, search, queues, time accounting. |
| `correlation.py` | 402 | **Zabbix ↔ Zammad** join on the webhook's ticket-id tag. |
| `opensearch_tool.py` | 280 | OpenStack service logs (`flog-*`). |
| `ceph_daemon_tool.py` | 264 | **New this session.** Ceph's daemon *containers* — see §7. |
| `vm_storage_correlation.py` | 196 | **Ceph ↔ Prometheus** join on the OpenStack instance UUID. |
| `zabbix_tool.py` | 109 | Thin dispatcher over `tools/zabbix/`. |
| `tools/zabbix/*` | 1,130 | One tool split by kind of question: client, common, metrics, hosts, items, monitoring, sla, audit. |

### `backend/static/` — the UI

Plain HTML, CSS and JS with no build step, served by FastAPI itself at `/`. There is
no second process and no CORS: the page and the API share an origin.

| File | Role |
|---|---|
| `index.html` | 168 lines. Sign-in gate; sticky top bar (History, Chat / Connections / Users tabs, spend meter, theme toggle, sign out); History drawer; three pages on a hash route. An inline `<head>` script applies the saved theme before first paint. |
| `app.css` | 1,056 lines. The whole design system. Every colour is a variable in `:root`; dark mode overrides them under `[data-theme="dark"]` and `prefers-color-scheme`. System font stacks (no CDN — private LAN). |
| `app.js` | 1,309 lines. `api()` (401 → sign-in gate), SSE reader over fetch (EventSource is GET-only), markdown, sortable tables, flow graph, treemap, **bar/line charts chosen from column names in `renderPayload`**, pricing and spend meter, credentials form, admin credits, History drawer and pins. |

Stack is deliberately plain: no React/bundler. The user once asked why there is no
separate frontend (Streamlit used to be one); answer given: one process, one origin, no
CORS, nothing to build — the API is still cleanly separate and a SPA could replace the
page later.

### `tests/`

| File | Lines | Role |
|---|---:|---|
| `test_units.py` | 1,769 | Offline tier (122 tests). Free, ~13s. Covers the database (schema, auth, sessions, credits, per-user isolation, turns, favourites, endpoint scoping) as well as the agent. One test carries `.mark.llm` and is held back by default. |
| `test_tools_live.py` | 855 | Live tool checks against the real systems. No model calls. |
| `routing_cases.py` | 208 | `question -> expected tool` corpus, in three kinds: unambiguous, collisions, and should-ask. |
| `score_routing.py` | 245 | Scores classify accuracy. Opt-in, spends tokens, `--only` narrows it. |
| `golden_queries.py` | 257 | Argument-level query sets and the team's real sample questions. |
| `test_extraction_llm.py` | 124 | NL → argument extraction evals. Paced, spends tokens. |
| `conftest.py` | 51 | Path + env bootstrap, auto-loaded by pytest. **Applies `credentials.json`**, and points the database at a scratch file so a test run never touches `iqcore.db`. |
| `pytest.ini` | — | At the repo root. `testpaths = tests` and `addopts = -m "not llm"`, so the default run never spends tokens. |

---

## 4. The correlation engines

Two pairs are joined. Everything else is not, for stated reasons.

### Zabbix ↔ Zammad (`correlation.py`)

Zabbix creates tickets in Zammad via webhook and writes the ticket id back onto the
Zabbix event as tag `__zbx_zammad_ticket_id`. That is a real foreign key. The engine
resolves the device, reads ticket ids off the tags, fetches only those tickets, joins
in pandas, and labels each row `EXACT` / `NO TICKET` / `DANGLING` with evidence.

**Why exactness matters:** it can assert an *absence*. "This device is down and nobody
raised a ticket" is the finding ops actually want.

**A trap that was avoided:** Zabbix has a host named `Windows` and OpenStack has a VM
named `windows`. Different machines (`192.168.1.104` vs `10.0.0.41`).

### Ceph ↔ Prometheus (`vm_storage_correlation.py`) — added this session

OpenStack names a Ceph RBD image after the instance: `<uuid>_disk` in the `vms` pool.
Prometheus carries the same UUID as `instance_id` on `libvirt_domain_openstack_info`.
Verified live before building:

```
Ceph  (vms pool)        1a05a978-3dd7-488a-8801-d234565a8ed6_disk
Prometheus instance_id  1a05a978-3dd7-488a-8801-d234565a8ed6  -> "jammy"
```

**Only 1 of 7 VMs matches.** The rest run on local hypervisor disk and are
structurally invisible to Ceph. Every VM gets a row anyway, labelled "not on Ceph" —
that absence is the finding for most VMs.

### What is NOT joined, and why

- **Prometheus ↔ Zammad** — impossible. Prometheus has zero alerting rules, so nothing
  there can raise a ticket.
- **Ceph ↔ Zammad** — no join key at all. Only Zabbix raises tickets.
- **Zabbix ↔ Prometheus** — possible in principle via IP, blocked because the exporter
  does not expose which VM owns which floating IP.

---

## 5. Routing: how it works and how it breaks

Classification is the most expensive operation in the app and the most common source
of wrong answers. This session rebuilt most of it.

**What classify sees:** one-line `ROUTING_SUMMARIES`, the `COLLISIONS` guidance, and
the question. Not the full docstrings — those cost ~2,829 tokens of parameter
documentation it cannot act on.

**Forced schema.** The decision comes back as a tool call whose `tool` field is an enum
of the real registered tools, so an invented tool name is unrepresentable. It retries
once on the larger model if the small one emits something malformed.

**Floor and ceiling.** `clarify.looks_ambiguous()` forces a clarifying question when
the sentence names nothing routable. `clarify.resolves_to_one_tool()` does the
opposite — undoes an over-eager ask when the vocabulary belongs to exactly one system.
Both stay silent on shared nouns.

**A real tool always outranks a contradicted ambiguity flag.** The schema does not
force `tool` and `ambiguous` to agree, and the model sometimes sets both. Code settles
it, checked last so the floor cannot re-assert.

### The vocabulary rule that matters most

`vocabulary.py` **documents, it never decides.** The word lists generate and check the
prompts; the model makes every routing call. A list that decides is static; a list that
documents is not.

**And the standing lesson:** a routing fix is only done when it has been verified
against a paraphrase sharing *no vocabulary* with the reported case. Fixing only the
literal reported phrasing is the treadmill that produced most of this session's bugs.

### Measuring it

`python tests/score_routing.py --only <group>` reports right-system, right-complexity,
how often it asked when it should have routed, and tokens per decision. **Run one group
at a time** — a full run is ~110,000 tokens.

---

## 6. Infrastructure findings

Discovered by probing the live systems. Several are gaps no amount of app work fixes.

**Prometheus has zero alerting rules.** Alertmanager runs with a `default-receiver` and
no integration. Nothing can fire, and no Prometheus→Zammad ticket can ever exist.

**Zabbix and Zammad run inside a VM this cluster hosts** (`192.168.1.205` is VM
`jammy` on server01). If server01 degrades, monitoring and ticketing degrade with it —
and Zabbix cannot see why, because it does not monitor the hypervisor.

**Log levels are inconsistently cased** — `error` 1,075,935 times, `ERROR` 23,657.
Counting one spelling is wrong by 45×. Handled in code.

**Access logs carry no level field**, so level counts never sum to the total.

**cAdvisor exposes no container names** — only opaque ids.

**No guest-OS label on VMs.** `os_type` is `hvm` for all seven; Windows-vs-Linux is
inferred from names and labelled as an inference.

**Ceph's settings endpoint returns a credential in plaintext.** `GET /api/settings`
includes `GRAFANA_API_PASSWORD` (value `admin`). `CephClient.SAFE_SETTINGS`
**whitelists five setting names** — a whitelist, not an output filter, because a filter
must anticipate every key a future release adds. Separately, Grafana at
`192.168.1.30:3000` answers its API **unauthenticated**. Infrastructure problem, needs
raising with whoever runs that host.

**Ceph's block-image endpoint pages silently.** Returned 5 images of 37 by default and
omitted the `volumes` pool entirely. `limit=-1` is mandatory. `GET /api/pool` reports
every pool at zero bytes without `stats=true`.

**Ceph's API versions are per-endpoint and not guessable.** The wrong version returns
**415**, which reads like a malformed request. `CEPH_API_VERSIONS` records them.

**Ceph's endpoints are genuinely flaky.** The same image call answered in 45s (timed
out), 25s, then 10.5s with nothing changed. This is why `request()` retries.

**Ceph crash reports are being dropped** — found this session. `ceph-crash` logs
`directory /var/lib/ceph/crash/posted does not exist; please create` every ~30 seconds,
728 times in six hours, while the cluster reports `HEALTH_OK`. **One `mkdir` on
server01 fixes it.** Invisible to every tool except `check_ceph_daemon_health`.

**Zabbix host inventory is switched off.** 10 of 11 hosts. EOL/EOS, model, serial and
OS version come from SNMP items instead. Zabbix holds no lifecycle data at all.

**Half the Zabbix estate has no audit trail.** Routers `.73/.74/.75` return 500 capped
rows; `Windows`, `.112` and `.80` return zero. Two live tests fail on `.80` for this
reason — environmental, not a code bug.

**Uptime lives under two different keys** — `system.net.uptime[sysUpTime.0]` for SNMP
kit, `system.uptime` for agent hosts.

**`sla.getsli` returns `sli = -1`** for periods before the SLA's effective date — a
sentinel, not a percentage.

**Zammad's search caps at 200 rows** whatever `limit` asks.

---

## 7. The second OpenSearch cluster

Added this session, and **it is not what it was described as**. Probed before building,
per §11 rule 1, which is the only reason the tool is useful.

`192.168.1.152:9200`, **HTTPS with a self-signed certificate** — plain HTTP is refused
at the socket in a way that reads like the host being down. Two indices configured:

**`docker-logs` (`OPENSEARCH2_ITSM_INDEX`)** — set aside. 16M documents but only two
fields (`@timestamp`, `log`), the `log` field a JSON string with no container identity
at all. 30% is OpenSearch logging about itself; Zabbix 1.3%, Zammad 0.6%, and the
"zabbix" matches are mostly Zammad ticket payloads mentioning Zabbix. **"Show me
Zabbix's logs" is not answerable from it.**

**`podman-20*` (`OPENSEARCH2_CEPH_INDEX`)** — **holds Ceph's container stdout, NOT the
Ceph cluster log.** Counted across the whole index: `HEALTH_OK`, `HEALTH_WARN`,
`slow ops`, `pg`, `mon`, `mgr`, `scrub` all occur **zero** times, while `query_ceph`
returned 60 real cluster entries at the same moment.

Of 61,000 documents, only ~4,800 are Ceph containers at all: 83% is `system:0`
heartbeat from ceph-exporter, and most of the rest is the one repeated crash error.

Three measured traps, all encoded in `ceph_daemon_tool.py`:

- **`PRIORITY` is not severity.** 60,513 of 61,219 documents are priority 3, because
  anything on stderr gets 3. A severity filter returns 99% of the index. The parameter
  is not offered, and a test asserts it never returns.
- **Aggregations fail silently on the wrong subfield.** Every field is `text` with a
  `.keyword` subfield; aggregating the bare name returns **empty buckets, not an
  error**.
- **Counts must not come from a sample.** Counting the newest 200 documents reported
  the crash error as "167 since 10:25" when the true figure is **728 since 05:47**.
  Counts come from an aggregation; `MESSAGE.keyword` has `ignore_above=256`, so the
  shortfall that leaves is measured and stated.

Coverage is **six hours, daily-rotated**. Every answer says so, because an empty result
for last week otherwise reads as "nothing happened".

---

## 8. Bugs worth knowing — they show the failure modes

**The model invents argument values from prompt examples.** `date_filter='last 10 days'`
appeared because that phrase sat in the tool's own example list. Fixed with
`arg_guard.py`.

**A dead parameter swallowed the question's subject.** Every tool accepted a `question`
argument and no tool body read it. **An unread parameter is worse than no parameter,
because it makes a call look scoped when it is not.**

**"Down" was a dead category.** Zabbix marks non-responding hosts `unreachable`.

**Multi-value arguments were silently dropped** by `if/elif` chains.

**`re.escape` broke PromQL** — `\.` is not a valid escape inside a PromQL string.

**libvirt sums CPU across vCPUs**, so a busy 16-vCPU VM read as 598%.

**Compressing tool descriptions broke extraction.** Those enumerations are **matching
hooks for the model, not documentation**. The prose around them is compressible; the
enumerations are not.

### Found and fixed this session

**Ceph reported our own trimming as missing data.** `/api/host` returns eight fields;
the table was built from a hand-written list of three, so the IP never arrived and the
model said *"no IP address is included in that payload"* — a claim about our column
list wearing Ceph's clothes. Rows now build from the payload's own keys.

**The classify prompt contradicted its own schema.** After switching to a forced tool
call, the prompt still ended with *"Respond with ONLY a JSON object"*. The model
obeyed the prompt, Groq rejected it as `tool_use_failed`, and the user saw "I could not
build a valid query for it" — while the model had produced the correct answer.

**A correct tool was discarded by its own ambiguity flag.** `tool=query_zammad` and
`ambiguous="system"` in the same response; the code only trusted the tool when the
model gave none. Placement of the fix mattered: clearing the axis before the
deterministic floor runs just lets the floor set it back.

**A VM name always needs Prometheus for Ceph questions.** "How much Ceph storage is
jammy using?" routed to `query_ceph` alone, which silently dropped "jammy" and answered
with cluster-wide capacity. Ceph cannot resolve a VM name — only pool names and UUIDs.

**Three endpoints held the event loop.** `/chat` (since removed), `/config` and
`/config/test` were `async def` while doing blocking work, so one request froze
everything including `/health`. Now plain `def`, which FastAPI runs in a worker thread.

**The offline tier was not free.** It advertised "no network" while one test ran the
compiled graph. That test now carries `@pytest.mark.llm`, and `pytest.ini` excludes
the marker by default.

**The loop guard was defeated by what followed it.** `LOOP_SUMMARY_CHARS` trimmed the
summary to 1,200, then an **untruncated** row preview was appended right after —
3,527 chars for the correlation table. Both are capped now.

---

## 9. Cost and limits

**Two Groq caps apply, and they fail differently.**

- **8,000 tokens/minute.** A simple question costs ~3,800–4,000, so roughly two per
  minute.
- **200,000 tokens/day on `gpt-oss-20b`.** Classify and extract both run on it, at
  ~2,900 per question — about **65–70 questions a day** before the app stops working.

The error names which cap was hit. Read it: one clears in seconds, the other does not.

**The orchestrator is at the edge of the per-minute limit.** It resends the system
prompt and **all nine tool schemas every iteration**:

```
9 tool schemas   17,568 chars  ~4,392 tokens
orchestrator      6,662 chars  ~1,665 tokens
                  -> ~6,057 tokens resent EVERY iteration
```

Adding the ninth tool pushed a four-step question from completing to a 413 at iteration
4. It was recovered by capping the row preview, but **the next tool will hit this
again.** This is the structural case for the plan node in §10.

**Measure, never estimate.** Character counting understated a simple question by
roughly half. `agent/telemetry.py` reports Groq's own figures per call.

---

## 10. Planned work

Detailed plans exist in `~/.claude/plans/groovy-mapping-rabbit.md`.

1. **A plan node for following instructions.** An engineer says "check this, and from
   that work out X"; the app makes a validated plan, shows it, and executes it with
   **no model call per step**. This is also the fix for §9 — a plan binds the schemas
   once instead of every iteration. Decided: show the plan and run it (no approval
   gate, because there is no resume mechanism), and resolve step-to-step references in
   code rather than re-planning.
2. **Paraphrase scoring.** Add paraphrases sharing minimal vocabulary to
   `routing_cases.py` and report sample-vs-paraphrase accuracy separately, so
   over-fitting to reported phrasings becomes a number.
3. **Split the two large tools.** `ceph_tool.py` (677) and `prometheus_tool.py` (595),
   the way `zabbix_tool.py` became `tools/zabbix/`.
4. **The three-way VM join** Ceph ↔ Prometheus ↔ OpenSearch. Two thirds now exists.
   Search logs for `"instance: <uuid>"`, **not** the bare UUID — the bare form matches
   ~569,000 routine polling lines per VM.
5. **Ceph historical logs** — revisit when `podman-*` holds weeks rather than hours.

### Blocked or impossible

- **Zabbix ↔ Prometheus by IP** — the exporter does not expose floating-IP ownership.
- **Prometheus ↔ Zammad** — needs alerting rules that do not exist.
- **Ceph ↔ Zammad** — no join key.

### Housekeeping

Add logging (there is still no `import logging` anywhere); pin dependencies; remove
hardcoded IP fallbacks; cap `raw` payloads in live responses (stored turns are already
trimmed). Auth: add password reset, lockout/rate limiting, and close open sign-up.
`auth_store.user_count()` is dead.

### The bigger question, honestly

Much of this session went into token golf — trimming schemas, capping previews,
counting tokens. **That is fighting a free-tier rate limit, not an architecture
problem.** Before more of it, check whether Groq offers prompt caching on tool schemas
and what a paid tier costs; either would make most of §9 disappear. Likewise, several
routing failures are small-model symptoms — `IQCORE_FAST_MODEL` is one environment
variable, and `score_routing.py` can measure whether a stronger model fixes them.

---

## 11. How to work on this codebase

1. **Probe before building.** Read the API reference for the *running* version, then
   query the live system to find what is actually populated, and build only for the
   overlap. The docs and reality disagreed on the highest-value item **every single
   time**: Zabbix inventory switched off, Ceph RGW not deployed, Zammad ticket links
   unused, and this session the "Ceph log index" that contains no cluster events.
2. **Prefer code to prompting** for anything checkable.
3. **Run the evals after every change** — `pytest -m "not live and not llm"` (~8s,
   free) while iterating; `pytest` before committing.
4. **Watch both budgets.** Test the tool layer directly; it is free.
5. **Keep the domain routing rules intact.** Blurring the systems produces confidently
   wrong answers. "How many VMs" is Prometheus, not Ceph.
6. **When a tool cannot answer, say so in the tool.** A misleading zero is worse than
   an error.
7. **Suspect silent truncation in every new API.** Zammad stops at 200; Ceph's image
   listing returned 5 of 37; OpenSearch stops counting at 10,000 and reports exactly
   that. Always check a count against a second source.
8. **A routing fix is not done until it generalises.** Verify against a paraphrase
   sharing no vocabulary with the reported case.
9. **Measure before optimising, and never estimate tokens.**
10. **Instrumentation must never cost an answer.**
11. **`conftest.py` applies `credentials.json` and uses a scratch database.** A script
    that touches the tools with nobody signed in reads the environment, so it needs
    `ensure_applied()`, or it runs unconfigured and reports "not authorized" as though
    the system were down.

---

## 12. Operational notes

**Credentials.** Each user's service credentials live in `backend/iqcore.db`,
encrypted with `backend/secret.key` (or `IQCORE_SECRET_KEY`); the Groq key stays in
`.env` / `credentials.json`. None of them are in git, so **a fresh clone has no
accounts and no credentials** — the first account created is the admin, and each user
connects their own systems on the **Connections** page, which can **Test** each
connection before saving. Back up `iqcore.db` and `secret.key` together.

**Ceph specifics.** `CEPH_URL` must be the API base (`https://192.168.1.30:8443`), not
the dashboard's browser address. `CEPH_VERIFY_TLS=false` for the self-signed cert.

**The second OpenSearch cluster** is HTTPS with a self-signed certificate.
`OPENSEARCH2_VERIFY_TLS=false`. Plain HTTP fails in a way that looks like an outage.

**Rotate the Prometheus and OpenSearch passwords** — they arrived via a Teams
screenshot.

**Raise the Grafana exposure** with whoever runs `192.168.1.30`.

**`mkdir /var/lib/ceph/crash/posted` on server01** — crash reports are being dropped.

**Testing costs tokens only in the `--llm` and `--routing` tiers.**

**Restart the backend after changing `.env`** — `load_dotenv()` runs at import.

**Windows note:** `pkill` does not exist; use `Stop-Process`. A stale uvicorn holding
port 8000 produces `WinError 10013`.

**Running it.** One process serves page and API:
`cd backend && uvicorn main:app --host 0.0.0.0 --port 8000` (on a VM), then open
`http://<host>:8000`. Locally, `.claude/launch.json` (config `iqcore`) runs the same
with `.venv/Scripts/python.exe` on 127.0.0.1:8000. Python lives in `.venv`; the system
Python has no pytest.

**Viewing the database.** Open `backend/iqcore.db` in DB Browser for SQLite (read-only
while the app runs). Service secrets show as ciphertext by design.

### Ticket assist (the only write) — Zammad-side setup

The current, step-by-step version (several Zammads, Ansible) is
[DEPLOYMENT.md](DEPLOYMENT.md) Part 4. The notes below record why each step exists.

Code: `backend/agent/ticket_assist.py`, `ZammadClient.add_note/add_tag`,
`POST /hooks/zammad/ticket-created`. Off by default; see the README section.

Found on the live Zammad (6.5.2) on 2026-10-09, and the reason for each step:

1. **Token** "IQ Core ticket assist", permission `ticket.agent` only ->
   `ZAMMAD_WRITE_TOKEN`. The app's read login is an Admin user; do not write with it.
2. **Webhook** to `http://<iqcore-host>:8000/hooks/zammad/ticket-created` with a
   signature token -> the same value in `ZAMMAD_WEBHOOK_SECRET`.
3. **Narrow trigger "2 New Article Created"**: it emails the ticket's customer the body
   of every article an Agent adds, internal or not. Add *Article visibility is public*,
   or the AI note is emailed out.
4. **Trigger** "IQ Core: suggest fix": *Action is created* and *Tags contains one not
   `iqcore-suggested`*, perform = the webhook.

Zabbix and people create tickets through the same account, so only the alert template
in the first article (`parse_zabbix_ticket_body`) tells them apart. Ticket links point
at Zabbix `192.168.1.180` while `ZABBIX_URL` is `192.168.1.205:8080`, which is why
version 1 does not look anything up in Zabbix. Not yet verified on a real ticket: the
exact `X-Hub-Signature` format, and that step 3 stops the customer email.
