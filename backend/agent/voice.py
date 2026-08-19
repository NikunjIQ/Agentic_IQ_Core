"""How answers are written.

The synthesis prompts used to say "Answer the user's question accurately...
Include actual values, counts, and names." That is a brief for a report, and the
output matched it — correct, complete, and nothing like a reply.

This asks for the same accuracy in the shape of a conversation. It is
deliberately terse: a NOC tool is read all shift by the same people, so warmth
here means being clear about what was checked and what could not be seen, not
pleasantries.
"""

# The orchestrator's system prompt is resent on every loop iteration, so the full
# text below would cost roughly 1,200 tokens on a ten-call question. Most complex
# answers come straight out of that loop rather than the closing call, so the
# style has to be there — but compactly.
CONVERSATIONAL_STYLE_BRIEF = (
    "When you answer: open by naming what you checked, give the real values, and "
    "close with one useful next step. State plainly if a table was truncated or a "
    "system held no data on the subject. No pleasantries, no restating the question."
)

# No worked example of the opening clause here on purpose. An earlier version
# showed "e.g. 'Checked Zabbix -'" and the model reproduced it verbatim on a
# Zammad question — "Checked Zabbix, there are 22 open tickets in Zammad". The
# system name is supplied as a fact instead, by system_for_tool below.
CONVERSATIONAL_STYLE = (
    "Write like a colleague replying, not like a report.\n"
    "- Open with a short clause naming the system stated below as the one you "
    "queried. Never name a system you did not query.\n"
    "- Give the answer with the real values, counts and names.\n"
    "- Close with ONE short suggested next step, and only when a genuinely useful "
    "one exists.\n"
    "- If the data was truncated, or a system returned nothing, say so in the answer "
    "itself. A system holding no data on a subject is not the same as nothing being "
    "wrong - be explicit about which it was.\n"
    "- No pleasantries, no 'Great question', do not restate the question. Keep the "
    "reply about as long as it would have been without these instructions."
)

# Which system each tool actually reads. Supplied to the synthesis prompt as a
# fact so the opening clause cannot name the wrong one.
_TOOL_SYSTEMS = {
    "query_zabbix": "Zabbix",
    "query_zammad": "Zammad",
    "query_prometheus": "Prometheus",
    "query_opensearch": "OpenSearch",
    "query_ceph": "Ceph",
    "correlate_problems_and_tickets": "Zabbix and Zammad",
    "lookup_ticket_context": "Zammad and Zabbix",
}


def system_for_tools(tools) -> str:
    """Human name(s) of the systems actually queried, for the opening clause."""
    if isinstance(tools, str):
        tools = [tools]
    names = []
    for tool in tools or []:
        name = _TOOL_SYSTEMS.get(tool)
        if name and name not in names:
            names.append(name)
    if not names:
        return ""
    return names[0] if len(names) == 1 else " and ".join(names)
