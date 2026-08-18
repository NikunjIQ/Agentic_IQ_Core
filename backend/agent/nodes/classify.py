import json
from langchain_groq import ChatGroq
from agent.state import AgentState
from agent.tool_registry import TOOL_REGISTRY
from agent.history import recent_turns
from agent import streaming, clarify

from agent.models import FAST_MODEL

CLASSIFY_MODEL = FAST_MODEL

CLASSIFY_PROMPT = """You are a fast triage classifier for an IT operations assistant.
Given a user's question, decide:

1. Is this a SIMPLE question, answerable with exactly ONE call to exactly ONE tool?
   Examples: "how many hosts are down", "how many tickets are open"
2. Or is this a COMPLEX question requiring MULTIPLE tool calls and reasoning across
   more than one system? Examples: "are there tickets related to hosts that are down",
   "what's the status of host X and any related tickets"

Available tools:
{tool_descriptions}

3. Is the question too vague to act on without guessing? If so, name the ONE axis that
   is unclear:
   - "system"    — cannot tell which system is meant (Zabbix network devices vs
                   Prometheus Linux servers vs Zammad tickets vs OpenSearch logs)
   - "scope"     — cannot tell which devices or group
   - "timeframe" — cannot tell what period
   Otherwise null.

   Set "ambiguous" to a value ONLY when the question names nothing to route on.
   Ambiguous: "what's down", "what's wrong", "is everything ok", "any issues",
   "show me the status" — no system, device, or object type is mentioned.

   Set it to null whenever the question names ANY of: a system (Zabbix, Prometheus,
   Zammad, OpenSearch), an object type (hosts, tickets, VMs, logs, containers),
   a device or IP, a host group, or a metric (CPU, memory, disk).
   Also null if the previous assistant message was a clarifying question — the
   user is answering it, not asking something new.

Respond with ONLY a JSON object, no other text:
{{
  "complexity": "simple" or "complex",
  "tool": "<tool name from the list above, ONLY if complexity is simple, otherwise null>",
  "ambiguous": "system" or "scope" or "timeframe" or null
}}
"""

def _classify_node_impl(state: AgentState) -> AgentState:
    streaming.status("Working out which system to check…")
    client = ChatGroq(model=CLASSIFY_MODEL, temperature=0)
    # Routing summaries, not full docstrings. This node picks a tool name; the
    # parameter documentation only matters once arguments are being extracted.
    tool_descriptions = "\n".join(f"- {name}: {entry['routing_summary']}"
                                  for name, entry in TOOL_REGISTRY.items())
    prompt = CLASSIFY_PROMPT.format(tool_descriptions=tool_descriptions)

    # A follow-up ("and server02?") is unroutable on its own — the system it
    # belongs to lives in the previous turn.
    response = client.invoke(
        [{"role": "system", "content": prompt}]
        + recent_turns(state.get("conversation_history"), max_turns=4)
        + [{"role": "user", "content": state["question"]}]
    )

    try:
        parsed = json.loads(response.content)
        state["complexity"] = parsed.get("complexity", "complex")
        state["simple_tool_choice"] = parsed.get("tool")
        axis = clarify.normalise_axis(parsed.get("ambiguous"))
    except (json.JSONDecodeError, AttributeError):
        # fail safe into the complex path rather than guessing — see Section 3's rationale
        state["complexity"] = "complex"
        state["simple_tool_choice"] = None
        axis = ""

    # Never ask twice in a row. If the previous turn was already a clarifying
    # question, this message is the answer to it — asking again would trap the
    # user in a loop, which is worse than guessing.
    if axis and clarify.was_just_asked(state.get("conversation_history")):
        axis = ""
    # A deterministic floor under the classifier. The small model is easily
    # talked out of asking — "show me what's down" reached Zabbix because an
    # example in this prompt told it broad questions were answerable. Code
    # catches the plainly vague cases whatever the model decided.
    elif not axis and clarify.looks_ambiguous(state["question"])             and not clarify.was_just_asked(state.get("conversation_history")):
        axis = "system"
    state["clarify_axis"] = axis

    return state


def classify_node(state: AgentState) -> AgentState:
    """Run the node, bracketed by the events that let a caller draw the run."""
    with streaming.node("classify"):
        return _classify_node_impl(state)
