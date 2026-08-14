from typing import TypedDict, Literal, Optional

class AgentState(TypedDict):
    question: str
    conversation_history: list[dict]
    complexity: Optional[Literal["simple", "complex"]]
    # Which axis was too vague to act on, if any — set by classify,
    # consumed by the clarify node.
    clarify_axis: Optional[str]
    simple_tool_choice: Optional[str]
    tool_results_raw: list[dict]
    dataframes: list  # pandas DataFrames
    tools_called: list[str]
    # What the answer could not see: truncated tables, systems not checked.
    coverage_notes: Optional[list]
    final_answer: Optional[str]
