from typing import TypedDict, Literal, Optional

class AgentState(TypedDict):
    question: str
    conversation_history: list[dict]
    complexity: Optional[Literal["simple", "complex"]]
    simple_tool_choice: Optional[str]
    tool_results_raw: list[dict]
    dataframes: list  # pandas DataFrames
    tools_called: list[str]
    final_answer: Optional[str]
