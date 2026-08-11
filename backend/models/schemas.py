from typing import Any
from pydantic import BaseModel

class ChatRequest(BaseModel):
    question: str
    conversation_history: list[dict] = []

class ChatResponse(BaseModel):
    final_answer: str
    dataframes: list[Any]
    tools_called: list[str]
    tool_results_raw: list[dict]
    complexity: str

