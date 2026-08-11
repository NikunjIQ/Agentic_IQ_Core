from agent.state import AgentState

def respond_node(state: AgentState) -> AgentState:
    # Currently a pass-through. Exists as a distinct node so future response
    # formatting, logging, or metrics collection has a clear single place to
    # live without touching either path's core reasoning logic.
    return state
