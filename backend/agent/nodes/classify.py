import json
from langchain_groq import ChatGroq
from agent.state import AgentState
from agent.tool_registry import TOOL_REGISTRY
from agent.history import recent_turns

CLASSIFY_MODEL = "llama-3.1-8b-instant"

CLASSIFY_PROMPT = """You are a fast triage classifier for an IT operations assistant.
Given a user's question, decide:

1. Is this a SIMPLE question, answerable with exactly ONE call to exactly ONE tool?
   Examples: "how many hosts are down", "how many tickets are open"
2. Or is this a COMPLEX question requiring MULTIPLE tool calls and reasoning across
   more than one system? Examples: "are there tickets related to hosts that are down",
   "what's the status of host X and any related tickets"

Available tools:
{tool_descriptions}

Respond with ONLY a JSON object, no other text:
{{
  "complexity": "simple" or "complex",
  "tool": "<tool name from the list above, ONLY if complexity is simple, otherwise null>"
}}
"""

def classify_node(state: AgentState) -> AgentState:
    client = ChatGroq(model=CLASSIFY_MODEL, temperature=0)
    tool_descriptions = "\n".join(f"- {name}: {entry['description']}" for name, entry in TOOL_REGISTRY.items())
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
    except (json.JSONDecodeError, AttributeError):
        # fail safe into the complex path rather than guessing — see Section 3's rationale
        state["complexity"] = "complex"
        state["simple_tool_choice"] = None

    return state
