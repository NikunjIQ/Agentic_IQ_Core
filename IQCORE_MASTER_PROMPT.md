# Agentic IQ Core — making the assistant feel conversational

You are working on **Agentic IQ Core** at
`C:\Users\NikunjSinha(Identiqa\Desktop\Agentic_IQ_Core`.

This brief covers **one piece of work only**: making the app behave like a conversational
assistant rather than a system that returns reports. Other planned work (token reduction,
a question catalogue) is out of scope here and will come as a separate brief. Read this
whole document before writing code.

---

## 1. What the app is

A natural-language assistant over four IT systems.

- **Backend**: FastAPI (`backend/main.py`)
- **Frontend**: Streamlit (`frontend/app.py`)
- **Orchestration**: LangGraph — `classify` → `simple_path` | `complex_path` → `respond`
  (`backend/agent/graph.py`)
- **LLM**: Groq. `llama-3.3-70b-versatile` for reasoning and synthesis,
  `llama-3.1-8b-instant` for classification and argument extraction.
- **Tools** (6, in `backend/agent/tool_registry.py`): `query_zabbix`, `query_zammad`,
  `query_prometheus`, `query_opensearch`, `correlate_problems_and_tickets`,
  `lookup_ticket_context`

Data domains, which the orchestrator prompt encodes and which must not be blurred:
Zabbix = network devices and Windows hosts. Prometheus = the Linux cluster
(server01/02/03) and its services. OpenSearch = OpenStack application logs only.
Zammad = the helpdesk.

Already built, and relevant here:

- `agent/streaming.py` — SSE status events plus token-by-token streaming. Working.
- `agent/history.py` — trims conversation turns (currently 6 turns, 1500 chars each).
- `agent/metadata.py` — `fetch_live_metadata()` returns live Zabbix host groups,
  Zammad ticket states and Prometheus nodes, cached 5 minutes. Use this as the source of
  concrete options when asking the user anything.
- `agent/arg_guard.py` — argument guarding, surfaces `guard_notes` to the UI.

---

## 2. The problem

Responsiveness and memory are not the issue — streaming works and history is passed. The
issue is that the app is prompted to report, not to converse. `simple_path.py:92`
instructs it to "Answer the user's question accurately… Include actual values, counts, and
names." That is a reporting brief, and the output matches it.

Four specific gaps, drawn from how comparable products behave:

**It guesses instead of asking.** The established pattern in conversational search is
mixed-initiative: when a request is ambiguous, the system takes the initiative to resolve
it rather than silently picking one interpretation. This app never asks. When it cannot
proceed it dead-ends — `simple_path.py:59-63` and `complex_path.py:171-174` both tell the
user to rephrase and state the system and time period explicitly. That hands the problem
back to the user.

**No acknowledge / answer / suggest rhythm.** Conversational UX guidance is consistent on
this: acknowledge what was said, give the answer, prompt the next step. This app does the
middle part only.

**No next steps.** Users repeatedly say they want an assistant to close the gap between
information and action rather than just display data. Comparable tools present guided
troubleshooting steps and let you refine an investigation through follow-ups in the same
conversation.

**Silence about its own limits.** When a table is truncated or a system returns nothing,
that only appears in the debug panel, not in the answer.

---

## 3. The work

Four changes, cheapest and highest-impact first. Stop after each and report before
continuing.

### 3.1 Replace the dead-ends with clarifying questions

The two failure messages at `simple_path.py:59-63` and `complex_path.py:171-174` should
become specific questions offering concrete options, not requests to rephrase.

- Draw the options from `fetch_live_metadata()` so they reflect the real environment —
  actual host groups, actual ticket states.
- Disambiguate along the axis that is actually unclear. The most common one is which
  system is meant: "Did you mean the routers and switches in Zabbix, or the Linux servers
  in Prometheus?" Time period is the next most common.
- Offer at most two or three options plus an escape ("something else — tell me more").
- The user's reply must resolve against the pending question. The existing
  `conversation_history` should carry enough context for this; if it does not, add a
  small pending-clarification field to the graph state rather than widening history.

Ask only when genuinely ambiguous. An assistant that asks a question every turn is worse
than one that guesses. If the question is answerable as written, answer it.

This is also token-positive: a clarifying question is a few dozen output tokens and avoids
a full wrong-path run, which is expensive on this project's Groq budget.

**Acceptance**: "show me what's down" produces a clarifying question with real host groups
in it; answering that question produces the correct query. "how many hosts are down in
Branch Routers" still answers directly with no extra turn.

### 3.2 Acknowledge, answer, suggest

Add one line to the synthesis prompts in `simple_path.py:92` and the complex-path closing
call: open with a brief acknowledgement of what was checked, close with a suggested next
step.

Keep it short. This is a NOC tool read all shift by the same people — one clause, not a
paragraph. No filler pleasantries, no "Great question!". Warmth here means being clear
about what was checked, what was found, and what could not be seen.

**Acceptance**: answers read as a reply to a person rather than a query result, and are
not materially longer than before.

### 3.3 Follow-up suggestion chips

Render clickable follow-up suggestions under each answer in `frontend/app.py`, derived
deterministically from `tools_called` in the response payload. No LLM call.

Examples: after a Zabbix problems query, offer "check for related tickets" and "show CPU
on these hosts"; after a Zammad ticket query, offer "what device raised this". Clicking a
chip submits it as the next user message.

Keep the mapping in one small module so it is easy to extend — `tools_called` plus, where
useful, whether the result was empty.

**Acceptance**: two or three relevant chips appear after an answer, clicking one runs it
as a normal question, and no extra model call is made to produce them.

### 3.4 Say what was not seen, in the answer itself

Truncation and empty results are currently only visible in the debug expander. They
belong in the answer.

- When a DataFrame was truncated for the model, the answer should say the figures cover
  only the rows it saw. The warning built at `complex_path.py:155-158` already carries
  this — make sure it reaches the user-facing text, not just the model.
- When a system returned nothing, distinguish "nothing is wrong" from "not collected
  here". This matters most for OpenSearch, which holds no network-device data at all: a
  zero result there means the data does not exist in that system, never that the network
  is healthy. This rule is already in `ORCHESTRATOR_SYSTEM_PROMPT` — surface it in the
  answer.
- When a question spanned systems and only some were checked, say which.

**Acceptance**: a query that truncates a table produces an answer that says so; an
OpenSearch query about a router says the data is not collected there rather than
reporting zero problems.

---

## 4. Constraints

1. **Read-only.** No writes to Zabbix, Zammad, Prometheus or OpenSearch.
2. **Do not blur the domain routing rules** in `ORCHESTRATOR_SYSTEM_PROMPT`. You may add
   to them; do not weaken them.
3. **Keep additions cheap.** The Groq free-tier budget on this project is tight — a
   complex query already costs roughly 28,000 tokens and can exceed the per-minute limit
   on its own. Prefer deterministic solutions to extra model calls. Item 3.3 in
   particular must cost nothing.
4. **No new dependencies without asking.**
5. Run `tests/test_units.py` and the eval harness after each change. Report results
   honestly, including failures.
6. Small commits, one item at a time.

---

## 5. Out of scope for this brief

Do not start these — they are planned separately:

- Question catalogue and tiered routing
- Token-reduction work (schema subsetting, prompt trimming, message pruning)
- Charts, forecasting, anomaly detection, daily digests
- Any write or remediation capability

---

## 6. How to report back

After each item: what changed file by file, before/after example exchanges showing the
difference in feel, and test plus eval results including failures. If something in this
brief does not match what you find in the code, trust the code and tell me.
