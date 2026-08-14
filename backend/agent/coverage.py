"""State what the answer could NOT see.

Truncation and empty results used to live only in the debug panel, so an answer
built from 10 of 48 rows read exactly like one built from all 48.

Everything here is computed in code and handed to the model as fact. The style
guidance already asks it to mention gaps, but that is a request; after watching
the model copy examples verbatim three times in this project, anything that must
appear in an answer is stated rather than requested.
"""

PREVIEW_ROWS = 10

# Which systems hold nothing at all about which subjects. A zero result here is
# a statement about the index, not about the health of the thing asked about.
_NOT_COLLECTED = {
    "query_opensearch": (
        ("cisco", "fastethernet", "interface", "router", "switch", "snmp",
         "192.168.1.7", "192.168.1.8", "link down", "port down"),
        "OpenSearch holds OpenStack application logs from server01/02/03 only — "
        "network devices never log there. This zero means the data is not collected "
        "in that system, NOT that the network is healthy. Zabbix is where those "
        "events live.",
    ),
    "query_prometheus": (
        ("ticket", "helpdesk", "sla", "agent assigned"),
        "Prometheus holds metrics, not tickets. This zero says nothing about the "
        "helpdesk — Zammad would have to be checked for that.",
    ),
}


def describe_table(frame, preview_rows: int = PREVIEW_ROWS) -> str:
    """Label a table preview with what it leaves out.

    Without the total, the model reads a truncated table as the whole result set
    and reports confident, undercounted figures.
    """
    if frame is None or getattr(frame, "empty", True):
        return ""
    total = len(frame)
    if total > preview_rows:
        header = (f"Data Table (showing {preview_rows} of {total} rows — do NOT count "
                  f"from this preview; use the totals in the summary above, and tell "
                  f"the user your list covers only the first {preview_rows})")
    else:
        header = f"Data Table (all {total} rows)"
    return f"\n\n{header}:\n{frame.head(preview_rows).to_string()}"


def empty_result_note(tool_name: str, question: str, summary: str) -> str:
    """A sentence explaining an empty result, when 'empty' could mislead.

    Returns "" when a zero is simply a zero and needs no explanation.
    """
    subjects, explanation = _NOT_COLLECTED.get(tool_name, ((), ""))
    haystack = f"{question} {summary}".lower()
    if subjects and any(word in haystack for word in subjects):
        return explanation
    return ""


# `raw` mixes results with metadata about the request. Counting the metadata as
# data made every OpenSearch result look non-empty, because raw always carries
# the query that produced it.
_RAW_METADATA_KEYS = {"query", "promql", "error", "host", "levels", "args"}


def looks_empty(result: dict) -> bool:
    frame = (result or {}).get("dataframe")
    if frame is not None and not getattr(frame, "empty", True):
        return False

    raw = (result or {}).get("raw") or {}
    if isinstance(raw.get("total"), int):
        return raw["total"] == 0
    for key, value in raw.items():
        if key in _RAW_METADATA_KEYS:
            continue
        if isinstance(value, (list, dict)) and value:
            return False
    return True


def coverage_note(tools_called, question: str = "") -> str:
    """Name the systems a cross-system question did NOT reach.

    "Is anything wrong?" answered from Zabbix alone is not wrong, but it is
    partial, and the answer should say so rather than imply the whole estate
    was checked.
    """
    tools = [t for t in (tools_called or []) if t and t != "clarify"]
    if not tools:
        return ""

    breadth = ("everything", "anything", "all systems", "whole", "entire estate",
               "any issues", "all of", "across")
    if not any(word in str(question).lower() for word in breadth):
        return ""

    checked = {
        "query_zabbix": "Zabbix (network devices, Windows)",
        "query_prometheus": "Prometheus (Linux servers, VMs)",
        "query_zammad": "Zammad (tickets)",
        "query_opensearch": "OpenSearch (OpenStack logs)",
        "correlate_problems_and_tickets": "Zabbix and Zammad",
        "lookup_ticket_context": "Zammad and Zabbix",
    }
    reached = {checked[t] for t in tools if t in checked}
    everything = set(checked.values()) - {"Zabbix and Zammad", "Zammad and Zabbix"}
    missed = everything - reached
    if not missed or not reached:
        return ""
    return (f"COVERAGE: this answer used {', '.join(sorted(reached))}. "
            f"Not checked: {', '.join(sorted(missed))}. Say so in the answer.")
