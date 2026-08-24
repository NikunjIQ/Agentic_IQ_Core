import json
from langchain_groq import ChatGroq
from agent.state import AgentState
from agent.tool_registry import TOOL_REGISTRY
from agent.tool_schemas import TOOL_SCHEMAS
from agent.metadata import fetch_live_metadata
from agent.arg_guard import scrub
from agent import streaming, clarify, coverage, telemetry
from agent.voice import (CONVERSATIONAL_STYLE, CONVERSATIONAL_STYLE_BRIEF,
                         system_for_tools)

from agent.models import REASONING_MODEL
# A ceiling against runaway loops, not a working budget — real questions resolve
# in two or three calls. Every iteration resends the system prompt and all tool
# schemas, so the old ceiling of 10 allowed ~47k tokens of overhead on a single
# question against an 8,000 tokens-per-minute allowance.
MAX_TOOL_CALLS = 5

# How much of a tool summary is carried back into the loop. Summaries deliberately
# carry the complete figures, which is right for the final answer but is resent on
# every subsequent iteration. The orchestrator only needs enough to decide what to
# do next; synthesis still receives the untruncated summary from tool_results_raw.
LOOP_SUMMARY_CHARS = 1200
PREVIEW_ROWS = 10

# The row preview is capped too, and this is not optional. LOOP_SUMMARY_CHARS
# above trims the summary, and then an UNTRUNCATED preview was appended right
# after it — so the guard was defeated by the thing that followed it. Measured
# on the correlation table: summary trimmed 2,703 -> 1,200, then a 3,527-char
# preview appended, for 4,727 chars entering the loop. That single append was
# most of the jump that made a four-step question exceed the 8,000 TPM limit.
#
# 500 rather than something more generous, and deliberately tighter than
# LOOP_SUMMARY_CHARS: the preview is only a SAMPLE, while the summary carries
# the complete figures the answer is built from, so trimming here costs
# context rather than correctness. Measured need — registering a ninth tool
# added ~430 tokens to every iteration and pushed a four-step question from
# completing to a 413, and this is where that margin was bought back.
LOOP_PREVIEW_CHARS = 500

ORCHESTRATOR_SYSTEM_PROMPT = """You are IQCore, an intelligent IT operations assistant.
You have access to five systems, each covering DIFFERENT infrastructure:
- Zabbix — network devices (routers, switches) and Windows hosts. Availability, SNMP, triggers.
- Prometheus — the Linux server cluster (server01/02/03) and the services on it: MySQL,
  RabbitMQ, Elasticsearch, HAProxy, memcached, ProxySQL, containers, OpenStack, HTTP probes.
- OpenSearch — LOG LINES from the OpenStack services (nova, neutron, keystone, glance,
  cinder, heat, rabbit, haproxy) running on server01/02/03.
- Zammad — the IT helpdesk: tickets, agents, SLA, states.
- Ceph — the STORAGE cluster under OpenStack: capacity, pools, OSDs, RBD images.

## RULE -1 — "logs" does NOT always mean OpenSearch
OpenSearch holds OpenStack APPLICATION logs only. It contains nothing about network
devices: no Cisco, no interface up/down, no SNMP.
- "logs for the OpenStack services / nova / neutron / keystone errors" → query_opensearch
- "logs for a network device down / interface down / router" → query_zabbix
  (query_type='problems' or 'alerts'); OpenSearch will return zero and that zero
  means "not collected here", never "nothing is wrong".
- "is a ceph daemon/service/container failing", "did ceph-exporter stop" →
  check_ceph_daemon_health. That reads what Ceph's CONTAINERS printed, which is
  a different thing from what the CLUSTER logged: a daemon can be broken while
  Ceph reports HEALTH_OK, and that fault appears nowhere else.
- "have any disks failed", "OSD status", "cluster health", "slow ops" →
  query_ceph. Those events do NOT exist in the container logs at all, so
  check_ceph_daemon_health would answer nothing and the zero would be
  meaningless rather than informative.

## RULE 0 — Send infrastructure questions to the RIGHT monitoring system
Zabbix and Prometheus do not watch the same machines. Choose by what is being asked about:
- routers, switches, SNMP devices, Windows hosts, 192.168.1.73/.74/.75 → query_zabbix
- server01/server02/server03, Linux hosts, MySQL, RabbitMQ, Elasticsearch, HAProxy,
  containers, OpenStack, exporters, scrape targets → query_prometheus
If a question asks about "all servers" or "everything", check BOTH and say which came
from which system. Never assume a host missing from one system does not exist — look in
the other before concluding anything.

## RULE 0b — Storage belongs to Ceph, not Prometheus
Prometheus watches VM and host CPU, memory and disk USAGE. Ceph watches the storage
layer itself. Choose by what is being asked about:
- free space, capacity, pools, OSDs, replication, RBD images, Cinder volumes,
  Glance images, storage health → query_ceph
- VM CPU/memory, host load, scrape targets, how many VMs exist → query_prometheus
"How many VMs" is NOT a Ceph question: Ceph only sees VMs whose disk is in its `vms`
pool, and VMs on local hypervisor storage are invisible to it. Ceph answers "which
VMs are Ceph-backed"; query_prometheus answers "how many VMs are there".

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

Ceph and Prometheus are linked the same way, on the OpenStack instance UUID Ceph
names its RBD image after and Prometheus carries as `instance_id`. For any question
touching BOTH a VM's storage AND its compute — "is jammy on Ceph and how busy is
it", "which VMs are Ceph-backed", "how much Ceph storage does this VM use" — call
`correlate_vm_storage_and_compute` ONCE rather than calling query_ceph and
query_prometheus separately and matching VM names yourself. Most VMs run on local
hypervisor disk, invisible to Ceph, and that absence is usually the actual answer —
a manual match would just show two tables with nothing obviously connecting them.

## RULE 2 — Single-system questions use the direct tools
- Network devices, availability, host counts → query_zabbix
- Linux servers, service metrics, scrape targets → query_prometheus
- Ticket volumes, SLA, agents, states → query_zammad
Correlation exists for Zabbix↔Zammad and Ceph↔Prometheus, and nowhere else.
Prometheus still has no ticket link — it has no alerting rules configured, so
nothing there can raise one — so for Prometheus-plus-ticket questions call
query_prometheus and query_zammad separately and say plainly that the two were
not joined automatically.

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

def _complex_path_node_impl(state: AgentState) -> AgentState:
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
    coverage_notes = []

    while tool_call_count < MAX_TOOL_CALLS:
        try:
            response = streaming.stream_with_tools(
                client.bind_tools(TOOL_SCHEMAS), messages,
                operation="orchestrate", model=REASONING_MODEL,
                detail=f"step {tool_call_count + 1}")
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

            streaming.tool_started(tool_name)
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

            df_context = coverage.describe_table(result.get("dataframe"), PREVIEW_ROWS)
            # Capped for the same reason the summary above is: this is what the
            # orchestrator carries forward to decide its NEXT call, and the
            # complete figures already live in the summary and in
            # tool_results_raw, which synthesis reads untruncated.
            if len(df_context) > LOOP_PREVIEW_CHARS:
                df_context = (df_context[:LOOP_PREVIEW_CHARS]
                              + f"\n[…preview trimmed for reasoning; {len(df_context)} "
                                f"chars total. Use the counts in the summary above, "
                                f"not this sample.]")

            # An empty result can mean "nothing is wrong" or "this system holds no
            # data of that kind at all". Those are very different answers, and only
            # the second one is dangerous to report as a clean bill of health.
            if coverage.looks_empty(result):
                note = coverage.empty_result_note(tool_name, state["question"],
                                                  str(result.get("summary", "")))
                if note:
                    df_context += "\n\nIMPORTANT — state this in your answer: " + note
                    coverage_notes.append(note)

            # Trimmed for the loop only. The row that says how many rows there
            # are sits at the front of every summary, so truncation costs detail
            # rather than the headline figure — and tool_results_raw still holds
            # the full text for the answer.
            loop_summary = result.get("summary", "No summary available")
            if len(loop_summary) > LOOP_SUMMARY_CHARS:
                loop_summary = (loop_summary[:LOOP_SUMMARY_CHARS]
                                + f" […truncated for reasoning; {len(loop_summary)} "
                                  f"chars total, full figures available]")

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "content": loop_summary + df_context,
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
    if coverage_notes:
        state["coverage_notes"] = coverage_notes
    return state


def complex_path_node(state: AgentState) -> AgentState:
    """Run the node, bracketed by the events that let a caller draw the run."""
    with streaming.node("complex_path"):
        return _complex_path_node_impl(state)
