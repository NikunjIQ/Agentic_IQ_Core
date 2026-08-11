from langgraph.graph import StateGraph, END
from agent.state import AgentState
from agent.nodes.classify import classify_node
from agent.nodes.simple_path import simple_path_node
from agent.nodes.complex_path import complex_path_node
from agent.nodes.respond import respond_node

def route_by_complexity(state: AgentState) -> str:
    return "simple_path" if state["complexity"] == "simple" else "complex_path"

def build_graph():
    graph = StateGraph(AgentState)

    graph.add_node("classify", classify_node)
    graph.add_node("simple_path", simple_path_node)
    graph.add_node("complex_path", complex_path_node)
    graph.add_node("respond", respond_node)

    graph.set_entry_point("classify")
    graph.add_conditional_edges("classify", route_by_complexity, {
        "simple_path": "simple_path",
        "complex_path": "complex_path",
    })
    graph.add_edge("simple_path", "respond")
    graph.add_edge("complex_path", "respond")
    graph.add_edge("respond", END)

    return graph.compile()

COMPILED_GRAPH = build_graph()
