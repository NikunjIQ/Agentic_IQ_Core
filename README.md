# Agentic IQ Core — IT Operations Assistant

A natural-language assistant over six IT data sources. Ask a question in plain
English; it works out which system holds the answer, queries it, and replies like a
colleague rather than returning a report.

```
"how many hosts are down"                    -> Zabbix
"which VM has the highest CPU?"              -> Prometheus
"how many tickets are open?"                 -> Zammad
"show me logs for today"                     -> OpenSearch
"how much storage is free?"                  -> Ceph
"is anyone working on the disk space issue?" -> Zabbix + Zammad, joined
"is jammy on Ceph and how busy is it?"       -> Ceph + Prometheus, joined
"is anything wrong with ceph's daemons?"     -> Ceph container logs
```

---

## What it connects to

| System | Covers | Notes |
|---|---|---|
| **Zabbix** | Network devices (Cisco routers, switches), Windows hosts | SNMP + agent |
| **Prometheus** | Linux cluster `server01/02/03`, OpenStack, VMs, containers | 53 scrape targets |
| **OpenSearch** | OpenStack application logs (`flog-*`) | ~5M docs/day, 60-day retention |
| **Zammad** | Helpdesk tickets, agents, SLA | Tickets auto-created by Zabbix |
| **Ceph** | Storage under OpenStack: pools, OSDs, RBD images | 19.2.3 squid; RGW not deployed |
| **OpenSearch #2** | Ceph's *container* stdout (`podman-*`) | HTTPS, self-signed. Not the cluster log |

These do **not** overlap, and the distinction matters. Zabbix watches the network kit;
Prometheus watches the Linux servers. OpenSearch holds OpenStack service logs and
nothing about network devices — a zero result there means "not collected here", never
"nothing is wrong".

---

## Architecture

```
Browser (backend/static/) — one page, no build step
     |  POST /chat/stream  (session cookie)
FastAPI (backend/main.py) — serves the API and the page
     |          \
     |           SQLite (backend/iqcore.db): users, sessions, credits, usage,
     |           per-user credentials (encrypted), saved turns, pinned questions
     |
LangGraph:  classify -> [ clarify | simple_path | complex_path ] -> respond
                              |            |            |
                              |            |            +-- orchestrator loop, up to 5 tool calls
                              |            +--------------- one tool, one call
                              +---------------------------- asks a question, no tools
                                           |
        9 tools -> Zabbix / Prometheus / OpenSearch / Zammad / Ceph / Ceph containers
```

**The design principle:** the LLM decides *what* to look up and writes the final
sentence. Everything in between — filtering, counting, joining, ranking — is ordinary
Python that either works or throws. Models are unreliable at arithmetic over data they
can only partly see; code is not.

### Models

| Job | Model | Why |
|---|---|---|
| Classification | `openai/gpt-oss-20b` | Small decision, cheap. Schema forced via `tool_choice` |
| Argument extraction | `openai/gpt-oss-20b` | Schema forced via `tool_choice` |
| Answer synthesis | `openai/gpt-oss-120b` | Prose quality |

Both ids live in `backend/agent/models.py`, overridable with `IQCORE_REASONING_MODEL`
and `IQCORE_FAST_MODEL`. Providers retire models — the previous pair were both
decommissioned mid-project, and the connection test checks the ids the app will
actually call so that shows up in the sidebar rather than mid-answer.

If the small model emits a malformed tool call, both classification and extraction
escalate to the larger one rather than failing the request.

---

## The tools

| Tool | What it answers |
|---|---|
| `query_zabbix` | hosts, host groups, items, problems, alerts, triggers, config audit, **what was changed in the 24h before each live problem** (people vs Zabbix's own template churn; hosts with no audit trail are named, not read as "nothing changed"), SLAs. Metrics cover CPU, memory, traffic, disk, uptime, OS/IOS version, agent version, hardware model/serial, ICMP latency and loss |
| `query_prometheus` | CPU/memory/disk/load/uptime/network — now, or as a trend over time (`history_minutes`, up to 24h) — scrape targets, VMs, OpenStack routers & networks, Cinder volumes, containers, raw PromQL, metric search |
| `query_opensearch` | OpenStack logs by date, text, level, service, host, tenant; breakdowns |
| `query_ceph` | health, capacity (raw **and** usable after replication), pools, OSDs, hosts, RBD images, snapshots, CephFS/MDS, CRUSH rules and hierarchy, cluster logs, monitoring endpoints |
| `query_zammad` | ticket counts, states, priorities, agents, organisations, SLA, tags, free-text search, named queues, booked time, one ticket's conversation, `group_by` |
| `correlate_problems_and_tickets` | Zabbix problems joined to their Zammad tickets |
| `lookup_ticket_context` | reverse: ticket -> device and originating problem |
| `correlate_vm_storage_and_compute` | a VM's Ceph disk joined to its Prometheus CPU/memory |
| `check_ceph_daemon_health` | Ceph's daemon *containers* — errors, liveness. **Not** disks or cluster health |

Every tool returns the same shape: a `summary` string carrying the **complete** figures,
a trimmed `dataframe` for display, and `raw` for the debug panel. The model sees only
10 preview rows, so the totals must live in the summary.

---

## The correlation engines

Two pairs are joined on real foreign keys, so each is a lookup rather than a guess.

**Zabbix ↔ Zammad.** Zabbix writes the ticket id back onto its own event as a tag
(`__zbx_zammad_ticket_id`):

```
device -> problems (+tags) -> ticket id -> fetch those tickets -> join in pandas
```

Each row carries **evidence** and a link tier — `EXACT`, `NO TICKET` (the genuinely
valuable finding), or `DANGLING`. Because it is exact, the tool can assert an
**absence**: "this device is down and has no ticket".

**Ceph ↔ Prometheus.** OpenStack names a Ceph RBD image after the instance
(`<uuid>_disk`), and Prometheus carries the same UUID as `instance_id`. Only 1 of 7 VMs
is Ceph-backed; the rest run on local hypervisor disk and are reported as such rather
than silently dropped.

**Not joined:** Prometheus↔Zammad is impossible (no alerting rules, so nothing can
raise a ticket) and Ceph↔Zammad has no join key at all.

---

## Setup

### Requirements

- Python 3.10+
- Network reach to your Zabbix / Zammad / Prometheus / OpenSearch / Ceph
- Ceph needs a username and password: the dashboard issues a short-lived token and
  offers no long-lived API key
- A Groq API key

### Install

```bash
git clone https://github.com/NikunjIQ/Agentic_IQ_Core.git
cd Agentic_IQ_Core
python3 -m venv venv && source venv/bin/activate
pip install -r backend/requirements.txt
```

### Configure

**Accounts.** Open the app and create an account — the first one ever created is the
admin. Accounts, credit balances, usage and each user's own service credentials live
in `backend/iqcore.db` (SQLite, gitignored). Service secrets inside it are encrypted
with a key from `IQCORE_SECRET_KEY`, or one generated into `backend/secret.key`
(gitignored). **Back up both files together** — the database's secrets are unreadable
without the key.

**Each user's systems.** On **Connections**, each user enters the systems *they*
use. A section left empty stays disconnected, and a question about that system says
so rather than borrowing anyone else's credentials. **Test** checks before **Save**.
That includes the Ceph container-log cluster (its own section) and the optional
Prometheus API token — neither is read from `.env` for a signed-in user.

**System settings.** The Groq key belongs to the deployment, not to a user: an admin
sets it on Connections, or put it in `backend/.env` (`cp backend/.env.example
backend/.env`). It is saved to `backend/credentials.json`.

An existing `authentication.json` and `credentials.json` are imported once, into an
empty database: users keep their passwords, and the old shared credentials become the
first user's.

| Variable | Default | What it does |
|---|---|---|
| `IQCORE_DB_PATH` | `backend/iqcore.db` | Where the database lives |
| `IQCORE_SECRET_KEY` | contents of `secret.key` | Key that encrypts service secrets |
| `IQCORE_PAISE_PER_TOKEN` | `2` | Selling price per token |
| `IQCORE_COOKIE_SECURE` | `false` | Set to `true` once TLS is in front |

### Run

```bash
cd backend && uvicorn main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000`. One process serves both the API and the page, so
there is no second server to start and no CORS to configure.

---

## Conversational behaviour

**It asks instead of guessing.** A question naming no system, device or metric gets a
question back, with options drawn from your live environment. Never twice in a row, and
never when the question is answerable as written.

**Answers read as replies.** They open by naming what was checked, give real values,
and close with one useful next step.

**Follow-up chips** appear under each answer, derived from which tools ran. No model
call, so they cost nothing.

**It says what it could not see** — truncated tables, systems not checked, and the
difference between "nothing is wrong" and "not collected here".

**Streaming**: `/chat/stream` sends status events during tool calls and tokens as the
answer is written.

---

## The page

Plain HTML/CSS/JS in `backend/static/`, no framework, no build step, no CDN (it runs
on a private LAN).

- **Sign-in gate.** Username + password (+ email on sign-up). A 401 anywhere brings it back.
- **Chat.** Each answer shows the agent's flow graph, the source system, the answer,
  sortable tables, token cost and its price in rupees.
- **Charts, picked from a result's columns** (not from which tool ran), so a new tool
  returning the right shape gets one for free:
  - `time` + `value` (+ `series`) → **line chart** with a hover crosshair readout.
  - a text column + numeric columns → **bar chart** with a "Chart by" select.
  - CRUSH hierarchy → **treemap**.
- **Dark mode.** Toggle in the top bar; follows the OS until chosen. Stored in
  `localStorage` and applied before first paint.
- **History drawer.** Every answered question is saved per account. Reopen a
  conversation (tables and charts redraw, follow-ups keep context) or delete it.
  **Pinned** questions (star beside "Asked") re-run in one click.
- **👍/👎 on every answer.** Stored per turn; admins list the thumbs-down at
  `/admin/feedback`, the source for new cases in `tests/routing_cases.py`.
- **Spend meter.** Total spent against a ₹10,000 ceiling. Representational only — never blocks.
- **Connections** page for each user's own credentials; **Users** page (admins) to allocate credit.

---

## Token telemetry

Every answer carries what it cost, because the models are capped and "that felt
expensive" is not something you can optimise.

```
🎫 3,207 tokens · 40% of a minute's allowance · 3 model call(s) · 3.2s
```

A **Token breakdown** expander shows where they went, per operation and per call, with
**thinking** tokens broken out separately — around 57% of output tokens on a typical
question are the model reasoning before it answers: billed, and never visible in the
reply.

Every figure comes from Groq's own `usage_metadata`. Nothing is estimated — an earlier
round of optimisation was costed by counting characters and understated a simple
question by roughly half.

A cache hit reports `0 tokens` with the original cost beside it. A call that returned
no usage figures is counted and named, so a missing measurement reads as a gap rather
than a smaller total.

---

## Dashboard — the front page

The page you land on after signing in. It shows each system's headline figures, read
straight from that system's API by `backend/agent/dashboard.py`. **No model is called**,
so opening or refreshing it costs no tokens.

| System (what it watches) | Figures |
|---|---|
| Zabbix (network devices, Windows hosts) | hosts up / down / unknown; active problems by severity |
| Zabbix + Zammad | active problems that have no ticket |
| Zammad (helpdesk) | open backlog, unassigned; created vs closed per day, 14 days |
| Prometheus (Linux servers, OpenStack VMs) | CPU / memory / disk per server; CPU over 24 h; scrape targets; VMs running |
| OpenSearch (OpenStack logs) | lines, errors and warnings today; errors per hour; noisiest services |
| Ceph (storage) | health and failing checks; raw capacity used; OSDs up; PGs clean; OSD map |
| Ceph daemons (container logs) | distinct problems; when each daemon was last heard from |

- **One request per system** (`GET /dashboard/<source>`), so a tile fills in as soon as
  its own system answers. One that is down, or not connected for the signed-in user,
  says so on its own tile and the rest still load.
- **Cached for 60 seconds per user**, refreshed every minute while the page is open.
  The Refresh button goes around the cache.
- **Click any figure or chart** to ask IQCore about it: Chat opens with the question.
- **Left off because they are slow:** Zammad SLA figures (need the full ticket
  download), Ceph images and snapshots, Zabbix change history.
- Charts are drawn with Apache ECharts 6.1.0, vendored at
  `backend/static/vendor/echarts.min.js` (Apache-2.0). Nothing is loaded from the
  internet; the page still works on an isolated LAN.

---

## Ticket assist — the one write

Everything above only reads. This is the exception: when Zabbix raises a ticket, IQ
Core adds a suggested fix to it as an **internal note**, so whoever picks the ticket up
starts with something more than the raw alert.

```
Zabbix alert -> Zammad ticket -> Zammad trigger -> signed webhook
   -> POST /hooks/zammad/ticket-created -> one model call -> internal note + tag
```

It ships **off**. `TICKET_ASSIST_MODE=dry_run` generates and logs the suggestion to
`backend/ticket_assist.jsonl` without writing; `write` also adds the note. To stop it,
set the mode back to `off` or disable the Zammad trigger.

- Only Zabbix alert tickets are touched; a ticket raised by a person is skipped.
- The note is labelled AI-generated. It is built from the alert text and general
  knowledge — the host is **not** inspected — so it is advice to verify, not a diagnosis.
- One note per ticket: the ticket is tagged `iqcore-suggested` and skipped after that.
- `TICKET_ASSIST_DAILY_LIMIT` (default 40) stops an alert storm spending the day's
  tokens; a repeated problem reuses its suggestion for 24 hours.
- The webhook must carry Zammad's HMAC signature (`ZAMMAD_WEBHOOK_SECRET`). Ticket
  assist acts as its own Zammad user through an agent-only token
  (`ZAMMAD_WRITE_TOKEN`), never through a signed-in user's credentials.

`python tests/replay_ticket_assist.py --limit 20` replays recent tickets with no writes
and saves the suggestions to a CSV for the team to score — the baseline for judging
any later change. Zammad-side setup is in `HANDOVER.md` §12.

---

## Guardrails

| Guard | Purpose |
|---|---|
| `arg_guard.py` | Strips filters the question never asked for (the model once invented `date_filter='last 10 days'`, turning 22 tickets into 2) |
| `coverage.py` | States truncation and empty-result meaning as fact |
| `vocabulary.py` | One source for routing words, so prompts cannot drift apart. Documents; never decides |
| Forced schemas | Classification and extraction return tool calls with enumerated names, so an invented tool is unrepresentable |
| HTTP retry | One retry on timeout or connection error — never on 4xx, and never on 429 |
| PromQL sanitiser | `x") or vector(1) #` becomes inert |
| Answer cache | 60s TTL; never caches errors; "refresh"/"again" bypasses it |

---

## Tests

```bash
pip install -r tests/requirements.txt

pytest -m "not live and not llm"   # offline wiring and unit checks (122), ~13s, free
pytest                             # + 74 live tool checks against the real systems (slow)
pytest -m ""                       # + extraction evals (SPENDS TOKENS)
python tests/score_routing.py      # score which system each question routes to
```

Three tiers, selected by marker: offline wiring and unit checks, `live` checks against
the real estate, and `llm` evals that spend tokens. `pytest.ini` sets
`addopts = -m "not llm"`, so **the default run never calls a model** — opting in is a
deliberate act.

`tests/score_routing.py` reports routing accuracy: right system, right complexity, how
often it asked when it should have routed, and tokens per decision. **Use `--only` to
score one group at a time** — a full run is ~110,000 tokens.

The consistency checks encode bugs that actually shipped: a docstring edit once changed
an answer from 22 to 3,784 with no error, and only an eval catches that.

---

## Known constraints

**Two Groq caps apply, and they fail differently.** 8,000 tokens/minute, and **200,000
tokens/day on `gpt-oss-20b`**. A simple question costs ~3,800, so roughly two per
minute and **65–70 per day**. The error names which cap was hit — one clears in
seconds, the other does not.

**The orchestrator is near the per-minute ceiling.** It resends all nine tool schemas
(~4,392 tokens) plus its system prompt on every iteration. Adding the ninth tool pushed
a four-step question into a 413 until the row preview was capped. The next tool will
hit this again.

**Providers retire models.** Both original models were decommissioned mid-project. Ids
live in `backend/agent/models.py` and are env-overridable.

**Zammad fetching is inefficient.** `query_zammad` downloads the whole ticket estate
(~3,825 tickets) before filtering for most queries. Cached 60s; a cold answer takes
~43s. Date, state, text and tag filters now run inside Zammad's own search,
paged past its 200-row limit, so a filtered question takes 1–6s. Past 1,000
matches (or if search fails) it downloads the estate instead, 8 pages at a
time (~16s). Owner, priority, customer and organisation are still filtered in
pandas: `priority.name` returned 0 with no error, so each field is pushed to
the server only once `test_zammad_server_filters_match_the_estate` proves it.
Dates are IST.

**Silent truncation is the recurring hazard.** Zammad's search returns at most 200
rows; Ceph's image listing returned 5 of 37 by default; OpenSearch stops counting at
10,000 and reports exactly that. Each is handled in code and stated in the answer, but
assume the next API does it too.

**Ceph's endpoints are flaky.** The same call has answered in 45s (timing out), 25s and
10.5s. HTTP calls retry once on timeout.

**Makeshift accounts.** Every data endpoint needs a session cookie, but sign-up is
open to anyone who can load the page, and there is no password reset, lockout or rate
limiting. Credit is shown, never enforced — a balance can go negative.

**Saved conversations are trimmed.** Each stored table is capped at 500 rows and the
raw tool output is dropped, so a reopened answer says its reasoning detail wasn't kept.

**The ticket-assist webhook sits outside the login gate.** Zammad cannot sign in, so
`/hooks/zammad/ticket-created` accepts only requests carrying Zammad's HMAC signature.

**No logging.** There is no record of what the agent did after the fact.

**Dependencies are unpinned.**

See `HANDOVER.md` for the full picture: what every file does, infrastructure findings,
every bug worth knowing, and what to build next.
