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
Streamlit (frontend/app.py)
     |  POST /chat/stream
FastAPI (backend/main.py)
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
| `query_zabbix` | hosts, host groups, items, problems, alerts, triggers, config audit, SLAs. Metrics cover CPU, memory, traffic, disk, uptime, OS/IOS version, agent version, hardware model/serial, ICMP latency and loss |
| `query_prometheus` | CPU/memory/disk/load/uptime/network, scrape targets, VMs, OpenStack routers & networks, Cinder volumes, containers, raw PromQL, metric search |
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
pip install -r frontend/requirements.txt
```

### Configure — two options

**Through the UI (recommended).** Start the app and use the sidebar. Enter each
system's details, press **Test** to verify, then **Save**. Written to
`backend/credentials.json` (gitignored).

**Through a file.** `cp backend/.env.example backend/.env` and fill it in.

Saved config takes precedence over `.env`; both are read, so they can be mixed.
Changing config clears every cache automatically.

### Run

```bash
cd backend && uvicorn main:app --host 0.0.0.0 --port 8000
```

```bash
cd frontend && streamlit run app.py --server.address 0.0.0.0 --server.port 8501
```

Open `http://localhost:8501`. Set `IQCORE_BACKEND_URL` if the backend is elsewhere.

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
python tests/run_evals.py --offline    # 111 checks, ~8s, free
python tests/run_evals.py              # + live tool checks against the real systems
python tests/run_evals.py --llm        # + extraction evals (spends tokens)
python tests/run_evals.py --routing    # score which system each question routes to
```

Three tiers: offline wiring and unit checks, live tool checks, and opt-in evals that
spend tokens. The offline tier is genuinely free — a module names any token-spending
test in `SPENDS_TOKENS` and the harness holds it back.

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
~43s. `tag_filter` and `search` skip the download.

**Silent truncation is the recurring hazard.** Zammad's search returns at most 200
rows; Ceph's image listing returned 5 of 37 by default; OpenSearch stops counting at
10,000 and reports exactly that. Each is handled in code and stated in the answer, but
assume the next API does it too.

**Ceph's endpoints are flaky.** The same call has answered in 45s (timing out), 25s and
10.5s. HTTP calls retry once on timeout.

**No authentication on the API.** Anyone who can reach port 8000 can query it and
change its configuration.

**No logging.** There is no record of what the agent did after the fact.

**Dependencies are unpinned.**

See `HANDOVER.md` for the full picture: what every file does, infrastructure findings,
every bug worth knowing, and what to build next.
