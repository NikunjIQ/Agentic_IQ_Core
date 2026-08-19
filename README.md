# Agentic IQ Core — IT Operations Assistant

A natural-language assistant over five IT systems. Ask a question in plain English;
it works out which system holds the answer, queries it, and replies like a colleague
rather than returning a report.

```
"how many hosts are down"                    -> Zabbix
"which VM has the highest CPU?"              -> Prometheus
"how many tickets are open?"                 -> Zammad
"show me logs for today"                     -> OpenSearch
"how much storage is free?"                   -> Ceph
"is anyone working on the disk space issue?" -> Zabbix + Zammad, joined
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

These do **not** overlap, and the distinction matters. Zabbix watches the network kit;
Prometheus watches the Linux servers. OpenSearch holds OpenStack service logs and
nothing about network devices — a zero result there means "not collected here", never
"nothing is wrong".

---

## Architecture

```
Streamlit (frontend/app.py)
     |  POST /chat  or  /chat/stream
FastAPI (backend/main.py)
     |
LangGraph:  classify -> [ clarify | simple_path | complex_path ] -> respond
                              |            |            |
                              |            |            +-- orchestrator loop, up to 5 tool calls
                              |            +--------------- one tool, one call
                              +---------------------------- asks a question, no tools
                                           |
                7 tools -> Zabbix / Prometheus / OpenSearch / Zammad / Ceph
```

**The design principle:** the LLM decides *what* to look up and writes the final
sentence. Everything in between — filtering, counting, joining, ranking — is ordinary
Python that either works or throws. Models are unreliable at arithmetic over data they
can only partly see; code is not.

### Models

| Job | Model | Why |
|---|---|---|
| Classification | `openai/gpt-oss-20b` | Small decision, cheap |
| Argument extraction | `openai/gpt-oss-20b` | Schema forced via `tool_choice` |
| Answer synthesis | `openai/gpt-oss-120b` | Prose quality |

Both ids live in `backend/agent/models.py` and can be overridden with
`IQCORE_REASONING_MODEL` and `IQCORE_FAST_MODEL`. Providers retire models — the
previous pair, `llama-3.3-70b-versatile` and `llama-3.1-8b-instant`, were both
decommissioned by Groq, and the connection test now checks the ids the app will
actually call so that shows up in the sidebar rather than mid-answer.

If the small model emits a malformed tool call, extraction escalates to the large one
rather than failing the request.

---

## The tools

| Tool | What it answers |
|---|---|
| `query_zabbix` | hosts, host groups, items, problems, alerts, triggers, config audit, SLAs and service levels. Metrics cover CPU, memory, traffic, disk, uptime, OS/IOS version, agent version, hardware model/serial, ICMP latency and loss |
| `query_prometheus` | CPU/memory/disk/load/uptime/network, scrape targets, VMs, OpenStack routers & networks, Cinder volumes, containers, raw PromQL, metric search |
| `query_opensearch` | OpenStack logs by date, text, level, service, host, tenant; breakdowns |
| `query_ceph` | storage cluster health, capacity (raw and usable), pools, OSDs, hosts, RBD images |
| `query_zammad` | ticket counts, states, priorities, agents, organisations, SLA, tags, `group_by` |
| `correlate_problems_and_tickets` | Zabbix problems joined to their Zammad tickets |
| `lookup_ticket_context` | reverse: ticket -> device and originating problem |

Every tool returns the same shape: a `summary` string carrying the **complete** figures,
a trimmed `dataframe` for display, and `raw` for the debug panel. The model sees only
10 preview rows, so the totals must live in the summary.

---

## The correlation engine

Zabbix creates tickets in Zammad and writes the ticket id back onto the Zabbix event as
a tag (`__zbx_zammad_ticket_id`). That tag is a real foreign key, so the join is a
lookup rather than a guess:

```
device -> problems (+tags) -> ticket id -> fetch those tickets -> join in pandas
```

Each row carries **evidence** and a link tier:

- `EXACT` — the tag points at a real ticket
- `NO TICKET` — nobody raised one (the genuinely valuable finding)
- `DANGLING` — the tag points at a deleted ticket

Because it is exact, the tool can assert an **absence** — "this device is down and has
no ticket" — which a model eyeballing 10 rows never can.

**Limitation:** the link is Zabbix↔Zammad only. Prometheus has no alerting rules
configured, so nothing there can raise a ticket.

---

## Setup

### Requirements

- Python 3.10+
- Network reach to your Zabbix / Zammad / Prometheus / OpenSearch / Ceph
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
question back, with options drawn from your live environment:

> Which set of devices did you mean?
> - Routers · Switches · Server · something else — just tell me more

Never twice in a row, and never when the question is answerable as written.

**Answers read as replies.** They open by naming what was checked, give real values,
and close with one useful next step.

**Follow-up chips** appear under each answer, derived from which tools ran. No model
call, so they cost nothing.

**It says what it could not see** — truncated tables, systems not checked, and the
difference between "nothing is wrong" and "not collected here".

**Streaming**: `/chat/stream` sends status events during tool calls and tokens as the
answer is written.

---

## Guardrails

| Guard | Purpose |
|---|---|
| `arg_guard.py` | Strips filters the question never asked for (the model once invented `date_filter='last 10 days'`, turning 22 tickets into 2) |
| `coverage.py` | States truncation and empty-result meaning as fact |
| PromQL sanitiser | `x") or vector(1) #` becomes inert |
| Error handling | Rate limits, connection failures and malformed calls become readable sentences, not 500s |
| Answer cache | 60s TTL; never caches errors; "refresh"/"again" bypasses it |

---

## Tests

```bash
python tests/run_evals.py
```

~98 checks in about 90 seconds. No pytest needed; works under pytest unchanged.

```bash
python tests/run_evals.py --offline    # no network, ~4s
python tests/run_evals.py --llm        # includes extraction evals (spends tokens)
```

Three tiers: offline wiring and unit checks, live tool checks against the real systems,
and opt-in NL→argument extraction evals. The consistency checks encode bugs that
actually shipped — a docstring edit once changed an answer from 22 to 3,784 with no
error, and only an eval catches that.

---

## Known constraints

**Groq free tier is the binding limit.** ~100k tokens/day. A complex question costs
roughly 13k, so about 7–11 per day. Test the connection through the sidebar — it uses
Groq's models endpoint and spends nothing.

**Zammad fetching is inefficient.** `query_zammad` downloads the whole ticket estate
(3,788 tickets, 38 pages) before filtering. Cached 60s, but a cold answer takes ~43s.
Zammad offers no count-only search, so this needs redesign rather than a flag.

**No authentication on the API.** Anyone who can reach port 8000 can query it and change
its configuration.

**No logging.** There is no record of what the agent did after the fact.

**Dependencies are unpinned.**

See `HANDOVER.md` for the full picture including infrastructure gaps and planned work.
