from agent.state import AgentState
from agent import streaming

def _respond_node_impl(state: AgentState) -> AgentState:
    # Currently a pass-through. Exists as a distinct node so future response
    # formatting, logging, or metrics collection has a clear single place to
    # live without touching either path's core reasoning logic.
    return state


def respond_node(state: AgentState) -> AgentState:
    """Run the node, bracketed by the events that let a caller draw the run."""
    with streaming.node("respond"):
        return _respond_node_impl(state)
