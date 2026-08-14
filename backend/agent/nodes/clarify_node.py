"""Ask the user one question instead of guessing or dead-ending.

No model call: the axis was decided during classification and the question text
is built from cached live values, so this node is free.
"""

from agent.state import AgentState
from agent import clarify, streaming


def clarify_node(state: AgentState) -> AgentState:
    question = clarify.build(state.get("clarify_axis") or "system", state["question"])

    # The text is not model output, so stream it explicitly or a streaming client
    # would sit blank until the run finished.
    emitter = streaming.get_emitter()
    if emitter is not None:
        emitter.token(question)

    state["final_answer"] = question
    state["tools_called"] = []
    state["tool_results_raw"] = [{"tool": "clarify",
                                  "args": {"axis": state.get("clarify_axis")},
                                  "result": {"summary": "asked the user to clarify"}}]
    state["dataframes"] = []
    return state
