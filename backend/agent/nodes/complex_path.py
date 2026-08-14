import json
from langchain_groq import ChatGroq
from agent.state import AgentState
from agent.tool_registry import TOOL_REGISTRY
from agent.tool_schemas import TOOL_SCHEMAS
from agent.metadata import fetch_live_metadata
from agent.arg_guard import scrub
from agent import streaming, clarify
from agent.voice import (CONVERSATIONAL_STYLE, CONVERSATIONAL_STYLE_BRIEF,
                         system_for_tools)

REASONING_MODEL = "llama-3.3-70b-versatile"
MAX_TOOL_CALLS = 10
PREVIEW_ROWS = 10

ORCHESTRATOR_SYSTEM_PROMPT = """You are IQCore, an intelligent IT operations assistant.
You have access to four systems, each covering DIFFERENT infrastructure:
- Zabbix — network devices (routers, switches) and Windows hosts. Availability, SNMP, triggers.
- Prometheus — the Linux server cluster (server01/02/03) and the services on it: MySQL,
  RabbitMQ, Elasticsearch, HAProxy, memcached, ProxySQL, containers, OpenStack, HTTP probes.
- OpenSearch — LOG LINES from the OpenStack services (nova, neutron, keystone, glance,
  cinder, heat, rabbit, haproxy) running on server01/02/03.
- Zammad — the IT helpdesk: tickets, agents, SLA, states.

## RULE -1 — "logs" does NOT always mean OpenSearch
OpenSearch holds OpenStack APPLICATION logs only. It contains nothing about network
devices: no Cisco, no interface up/down, no SNMP.
- "logs for the OpenStack services / nova / neutron / keystone errors" → query_opensearch
- "logs for a network device down / interface down / router" → query_zabbix
  (query_type='problems' or 'alerts'); OpenSearch will return zero and that zero
  means "not collected here", never "nothing is wrong".

## RULE 0 — Send infrastructure questions to the RIGHT monitoring system
Zabbix and Prometheus do not watch the same machines. Choose by what is being asked about:
- routers, switches, SNMP devices, Windows hosts, 192.168.1.73/.74/.75 → query_zabbix
- server01/server02/server03, Linux hosts, MySQL, RabbitMQ, Elasticsearch, HAProxy,
  containers, OpenStack, exporters, scrape targets → query_prometheus
If a question asks about "all servers" or "everything", check BOTH and say which came
from which system. Never assume a host missing from one system does not exist — look in
the other before concluding anything.

## RULE 1 — Cross-system questions use the correlation tool, not manual joining

Zabbix and Zammad are already linked: when monitoring raises a problem it creates a
Zammad ticket and records that ticket's id on the Zabbix event. `correlate_problems_and_tickets`
follows that link in code and returns a finished table.

So if a question touches BOTH devices/problems AND tickets, call
`correlate_problems_and_tickets` ONCE and answer from its table. Examples:
- "show me tickets for 192.168.1.73 and the main problem on it"
- "which problems have no ticket raised"
- "is anyone working on the disk space issue"

Do NOT call query_zabbix and query_zammad separately and try to match hostnames yourself.
You cannot see every row, so a manual match will silently miss data. The correlation tool
sees all of it.

To go the other way — from a ticket to the device that caused it — use `lookup_ticket_context`.

## RULE 2 — Single-system questions use the direct tools
- Network devices, availability, host counts → query_zabbix
- Linux servers, service metrics, scrape targets → query_prometheus
- Ticket volumes, SLA, agents, states → query_zammad
The correlation tool links Zabbix to Zammad only. Prometheus has no ticket link, so for
Prometheus-plus-ticket questions call query_prometheus and query_zammad separately and
say plainly that the two were not joined automatically.

## RULE 3 — Answer from the data, then stop
- Once you have the table you need, STOP calling tools and answer.
- Quote real values: hostnames, ticket numbers, states, severities, counts.
- The correlation table has a `link` column: EXACT means the ticket is definitively the
  one monitoring raised for that problem. NO TICKET means nobody has raised one — say so
  plainly, it is important. DANGLING means the ticket was deleted.
- If a table says it was truncated, say your answer covers only the rows you saw.
- If you genuinely cannot answer, say so clearly.

## RULE 4 — How the answer should read
{style}

Live System Schema (use EXACT values from this list when extracting arguments):
{live_schema}
"""

def complex_path_node(state: AgentState) -> AgentState:
    client = ChatGroq(model=REASONING_MODEL, temperature=0)

    live_schema = fetch_live_metadata()
    system_content = ORCHESTRATOR_SYSTEM_PROMPT.format(
        live_schema=live_schema, style=CONVERSATIONAL_STYLE_BRIEF)

    messages = [{"role": "system", "content": system_content}]
    messages += state["conversation_history"]
    messages.append({"role": "user", "content": state["question"]})

    tools_called = []
    tool_results_raw = []
    dataframes = []
    call_cache = {}
    tool_call_count = 0
    final_answer = None

    malformed_calls = 0
    guard_notes = []

    while tool_call_count < MAX_TOOL_CALLS:
        try:
            response = streaming.stream_with_tools(
                client.bind_tools(TOOL_SCHEMAS), messages)
        except Exception as exc:
            # The model sometimes emits a tool call Groq cannot parse. That is a
            # bad generation, not a bad question — retry once, and if it happens
            # again answer from whatever has already been gathered rather than
            # losing the entire request.
            if "tool_use_failed" not in str(exc):
                raise
            malformed_calls += 1
            if malformed_calls == 1:
                messages.append({
                    "role": "user",
                    "content": ("Your last tool call was malformed. Emit exactly one "
                                "well-formed tool call, omitting any argument you do "
                                "not need rather than passing null."),
                })
                continue
            break

        if not response.tool_calls:
            final_answer = response.content
            break

        messages.append(response)
        for tool_call in response.tool_calls:
            tool_name = tool_call["name"]
            # Same guard as the simple path: a time window the question never
            # mentioned narrows the answer without anyone noticing.
            tool_args, dropped = scrub(tool_call["args"], state["question"],
                                       state.get("conversation_history"))
            if dropped:
                guard_notes.extend(f"{tool_name}: removed unrequested {d}" for d in dropped)
            tools_called.append(tool_name)
            tool_call_count += 1

            streaming.status(f"Querying {tool_name.replace('query_', '').replace('_', ' ').title()}…")
            tool_entry = TOOL_REGISTRY.get(tool_name)
            call_key = (tool_name, json.dumps(tool_args, sort_keys=True, default=str))

            if tool_entry is None:
                result = {"summary": f"Unknown tool: {tool_name}", "dataframe": None, "raw": None}
            elif call_key in call_cache:
                # The model sometimes re-issues an identical call instead of answering.
                # Replaying the cached result keeps it from burning the tool budget and
                # re-hitting Zabbix/Zammad for data it already has.
                result = call_cache[call_key]
            else:
                try:
                    result = tool_entry["function"].invoke(tool_args)
                except Exception as e:
                    result = {"summary": f"Tool '{tool_name}' raised an error: {str(e)}", "dataframe": None, "raw": {"error": str(e)}}
                call_cache[call_key] = result

            tool_results_raw.append({"tool": tool_name, "args": tool_args, "result": result})
            if result.get("dataframe") is not None:
                dataframes.append(result["dataframe"])

            df_context = ""
            frame = result.get("dataframe")
            if frame is not None and not frame.empty:
                # Say so when rows are being withheld. Without this the model reads a
                # truncated table as the whole result set and reports confident,
                # undercounted answers.
                if len(frame) > PREVIEW_ROWS:
                    header = (f"Data Table (showing {PREVIEW_ROWS} of {len(frame)} rows — "
                              f"do NOT count from this preview, use the totals in the summary above)")
                else:
                    header = f"Data Table (all {len(frame)} rows)"
                df_context = f"\n\n{header}:\n{frame.head(PREVIEW_ROWS).to_string()}"

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "content": result.get("summary", "No summary available") + df_context,
            })

    if final_answer is None:
        if malformed_calls > 1 and not tool_results_raw:
            # Nothing was gathered and the model cannot produce a valid call.
            # Ask which system was meant instead of asking the user to rephrase.
            state["final_answer"] = clarify.build("system", state["question"])
            emitter = streaming.get_emitter()
            if emitter is not None:
                emitter.token(state["final_answer"])
            state["tools_called"] = tools_called
            state["tool_results_raw"] = tool_results_raw
            state["dataframes"] = dataframes
            return state
        closing = ("Answer the question from the tool results gathered so far."
                   if malformed_calls else
                   "You've reached the maximum tool calls. Summarize what you found so far.")
        # The full style guidance is affordable here — this closing call happens
        # once, unlike the system prompt which is resent every iteration.
        messages.append({"role": "user",
                         "content": f"{closing}\n\n{CONVERSATIONAL_STYLE}"})
        streaming.status("Writing the answer…")
        final_answer = streaming.stream_text(client, messages)

    state["final_answer"] = final_answer
    state["tools_called"] = tools_called
    state["tool_results_raw"] = tool_results_raw
    state["dataframes"] = dataframes
    if guard_notes:
        state["guard_notes"] = guard_notes
    return state
