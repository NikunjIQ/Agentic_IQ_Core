"""Follow-up suggestions, derived from which tools ran.

Deterministic on purpose — no model call, no tokens. The whole point of a chip
is that it is free to produce; if it cost a model call it would be cheaper to
let the user type.

Each suggestion must be a question this app can actually answer, phrased the way
someone would type it. A chip that leads to "I could not work that out" is worse
than no chip.
"""

MAX_SUGGESTIONS = 3

# tool -> follow-ups that make sense once that tool has run.
_AFTER_TOOL = {
    "query_zabbix": [
        "Are there tickets open for these devices?",
        "What are the most severe problems right now?",
    ],
    "query_zammad": [
        "Which device raised these tickets?",
        "Which agent has the most open tickets?",
    ],
    "query_prometheus": [
        "Which VM has the highest resource utilisation?",
        "Are all the scrape targets healthy?",
    ],
    "query_opensearch": [
        "Show only the errors",
        "Which service logged the most entries?",
    ],
    "correlate_problems_and_tickets": [
        "Which problems have no ticket raised?",
        "Show the most severe problem and its ticket",
    ],
    "lookup_ticket_context": [
        "What else is wrong on that device?",
        "Are there other tickets for the same device?",
    ],
}

# When a query came back with nothing, the useful next move is to widen it
# rather than to explore sideways.
_AFTER_EMPTY = {
    "query_opensearch": ["Widen the search to the last 7 days",
                         "Show all log levels, not just errors"],
    "query_zammad": ["Show tickets in every state", "Widen the time period"],
    "query_zabbix": ["Show all hosts, not just the filtered ones"],
    "query_prometheus": ["List every scrape target and its health"],
}


def suggest(tools_called, result_was_empty: bool = False) -> list:
    """Two or three follow-ups for the tools that just ran.

    Returns nothing for a clarifying turn — the app has just asked the user a
    question, and offering unrelated chips underneath it competes with it.
    """
    tools = [t for t in (tools_called or []) if t]
    if not tools or "clarify" in tools:
        return []

    pool = _AFTER_EMPTY if result_was_empty else _AFTER_TOOL

    suggestions = []
    for tool in tools:
        for option in pool.get(tool, []):
            if option not in suggestions:
                suggestions.append(option)

    # An empty result may have no widening advice for that tool; fall back to the
    # normal follow-ups rather than showing nothing.
    if not suggestions and result_was_empty:
        for tool in tools:
            for option in _AFTER_TOOL.get(tool, []):
                if option not in suggestions:
                    suggestions.append(option)

    return suggestions[:MAX_SUGGESTIONS]


def looks_empty(dataframes, tool_results_raw) -> bool:
    """Whether the answer was built from no rows at all.

    Used only to choose between widening and exploring, so a rough signal is
    enough — it never changes the answer itself.
    """
    for frame in dataframes or []:
        if isinstance(frame, list) and frame:
            return False
        if hasattr(frame, "empty") and not frame.empty:
            return False
    for entry in tool_results_raw or []:
        summary = str((entry.get("result") or {}).get("summary", "")).lower()
        if summary and not any(marker in summary for marker in
                               ("no ", "0 ", "none", "not found", "nothing")):
            return False
    return True
