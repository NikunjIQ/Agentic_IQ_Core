from agent.tool_registry import LANGCHAIN_TOOLS
from langchain_core.utils.function_calling import convert_to_openai_function

def build_tool_schemas() -> list[dict]:
    return [
        {
            "type": "function",
            "function": convert_to_openai_function(tool_obj)
        }
        for tool_obj in LANGCHAIN_TOOLS
    ]

TOOL_SCHEMAS = build_tool_schemas()

