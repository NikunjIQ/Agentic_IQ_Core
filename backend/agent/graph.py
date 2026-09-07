from langgraph.graph import StateGraph, END
from agent.state import AgentState
from agent import config_store

# Importing the graph means the app is about to run, and the model client reads
# GROQ_API_KEY at construction time. Credentials saved through the UI live in
# credentials.json, so without this any entry point other than main.py builds a
# client with no key at all.
config_store.ensure_applied()
from agent.nodes.classify import classify_node
from agent.nodes.simple_path import simple_path_node
from agent.nodes.complex_path import complex_path_node
from agent.nodes.clarify_node import clarify_node

def route_by_complexity(state: AgentState) -> str:
    # A vague question is answered with a question, not a guess.
    if state.get("clarify_axis"):
        return "clarify"
    return "simple_path" if state["complexity"] == "simple" else "complex_path"

def build_graph():
    graph = StateGraph(AgentState)

    graph.add_node("classify", classify_node)
    graph.add_node("simple_path", simple_path_node)
    graph.add_node("complex_path", complex_path_node)
    graph.add_node("clarify", clarify_node)

    graph.set_entry_point("classify")
    graph.add_conditional_edges("classify", route_by_complexity, {
        "simple_path": "simple_path",
        "complex_path": "complex_path",
        "clarify": "clarify",
    })
    graph.add_edge("clarify", END)
    graph.add_edge("simple_path", END)
    graph.add_edge("complex_path", END)

    return graph.compile()

COMPILED_GRAPH = build_graph()
