"""Natural-language -> tool-argument accuracy. Costs tokens; opt in with --llm.

This is the layer that regresses invisibly. Editing one sentence of a docstring
once changed "How many tickets are in Open State?" from 22 tickets to 3,784,
with no code change and no error. Only an eval catches that.

Each case asserts on the ARGUMENTS the model produces, not on the prose it
writes, because arguments are checkable and prose is not.
"""

import time

import conftest  # noqa: F401

REQUIRES_LLM = True

# (question, tool, {argument: expected value or None meaning "must be absent"})
EXTRACTION_CASES = [
    # Zabbix — multi-value arguments must survive intact
    ("Please share total devices which are down",
     "query_zabbix", {"query_type": "hosts", "status_filter": "down"}),
    ("List all hosts currently in unreachable or unknown status",
     "query_zabbix", {"query_type": "hosts"}),
    ("What is the current cpu & memory utilization of 192.168.1.73?",
     "query_zabbix", {"query_type": "items", "host_filter": "192.168.1.73"}),
    ("Tell me the problem with 192.168.1.74",
     "query_zabbix", {"query_type": "problems", "host_filter": "192.168.1.74"}),
    ("When was the config of 192.168.1.80 changed?",
     "query_zabbix", {"query_type": "audit", "host_filter": "192.168.1.80"}),

    # Zammad — the two guards that were added after real misreads
    ("How many tickets are in Open State?",
     "query_zammad", {"state_filter": "open"}),
    ("How many tickets are Work in Progress?",
     "query_zammad", {"state_filter": "Work in Progress"}),
    ("Which agent has resolved the most tickets?",
     "query_zammad", {"date_filter": None}),          # must NOT invent a window
    ("How many tickets are closed in last 10 days?",
     "query_zammad", {"date_field": "closed"}),
    ("How many tickets are marked as priority 2?",
     "query_zammad", {"priority_filter": "priority 2"}),

    # Prometheus — VM shortcuts and the router ambiguity
    ("Please share total VMs running", "query_prometheus", {"metric": "vms"}),
    ("Which VM has the highest resource utilization currently?",
     "query_prometheus", {"metric": "vm_utilization"}),
    ("How many VMs are on private network and how many on public?",
     "query_prometheus", {"metric": "networks"}),
]


def _extract(question: str, tool_name: str) -> dict:
    from langchain_groq import ChatGroq
    from agent.tool_schemas import TOOL_SCHEMAS
    from agent.metadata import fetch_live_metadata
    from agent.nodes.simple_path import FAST_MODEL

    schema = next(t for t in TOOL_SCHEMAS if t["function"]["name"] == tool_name)
    system = ("You are an argument extractor. Use the following live system schema to "
              "precisely map your arguments to the user's request. Do not hallucinate "
              "values outside this schema.\n\n" + fetch_live_metadata())
    client = ChatGroq(model=FAST_MODEL, temperature=0).bind_tools(
        [schema], tool_choice=tool_name)
    response = client.invoke([{"role": "system", "content": system},
                              {"role": "user", "content": question}])
    return response.tool_calls[0]["args"] if response.tool_calls else {}


def test_argument_extraction_matches_expectations():
    failures = []
    for question, tool, expectations in EXTRACTION_CASES:
        try:
            args = _extract(question, tool)
        except Exception as exc:
            failures.append(f"{question!r}: extraction failed — {str(exc)[:120]}")
            continue

        for key, expected in expectations.items():
            actual = args.get(key)
            if expected is None:
                if actual not in (None, ""):
                    failures.append(
                        f"{question!r}: invented {key}={actual!r} (should be absent)")
            elif actual is None or expected.lower() not in str(actual).lower():
                failures.append(f"{question!r}: {key} expected ~{expected!r}, got {actual!r}")
        time.sleep(1)  # stay inside the per-minute token allowance

    assert not failures, "extraction regressions:\n  " + "\n  ".join(failures)


def test_followup_question_resolves_against_history():
    """The simple path used to drop history, so follow-ups silently broke."""
    from langchain_groq import ChatGroq
    from agent.tool_schemas import TOOL_SCHEMAS
    from agent.history import recent_turns
    from agent.nodes.simple_path import FAST_MODEL

    history = [
        {"role": "user", "content": "What is the CPU utilization of server01?"},
        {"role": "assistant", "content": "server01 is at 18.5% CPU."},
    ]
    schema = next(t for t in TOOL_SCHEMAS if t["function"]["name"] == "query_prometheus")
    client = ChatGroq(model=FAST_MODEL, temperature=0).bind_tools(
        [schema], tool_choice="query_prometheus")
    response = client.invoke(
        [{"role": "system", "content": "You are an argument extractor."}]
        + recent_turns(history)
        + [{"role": "user", "content": "and what about server02?"}])

    args = response.tool_calls[0]["args"] if response.tool_calls else {}
    assert "server02" in str(args.get("host_filter", "")).lower(), (
        f"follow-up did not resolve to server02: {args}")
