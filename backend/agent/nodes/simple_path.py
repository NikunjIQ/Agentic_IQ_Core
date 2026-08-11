import json
from datetime import datetime, timezone
from langchain_groq import ChatGroq
from agent.state import AgentState
from agent.tool_registry import TOOL_REGISTRY
from agent.tool_schemas import TOOL_SCHEMAS
from agent.metadata import fetch_live_metadata

SYNTHESIS_MODEL = "llama-3.3-70b-versatile"
# Argument extraction is a constrained task — the schema is forced via tool_choice,
# so the model only fills in fields. The small model is sufficient here and keeps
# the simple path from spending 70B tokens twice per question.
FAST_MODEL = "llama-3.1-8b-instant"

def simple_path_node(state: AgentState) -> AgentState:
    tool_name = state["simple_tool_choice"]
    tool_entry = TOOL_REGISTRY.get(tool_name)

    if tool_entry is None:
        state["final_answer"] = "I wasn't able to determine which system to check for this question. Could you rephrase it?"
        return state

    # Extract dynamic arguments using the fast model
    tool_schema = next((t for t in TOOL_SCHEMAS if t.get("function", {}).get("name") == tool_name), None)
    tool_args = {"question": state["question"]}
    
    if tool_schema:
        live_schema = fetch_live_metadata()
        system_msg = f"You are an argument extractor. Use the following live system schema to precisely map your arguments (e.g. host groups, ticket states) to the user's request. Do not hallucinate values outside this schema if a strict match is required.\n\n{live_schema}"
        
        fast_client = ChatGroq(model=FAST_MODEL, temperature=0)
        bound_client = fast_client.bind_tools([tool_schema], tool_choice=tool_name)
        response = bound_client.invoke([
            {"role": "system", "content": system_msg},
            {"role": "user", "content": state["question"]}
        ])
        if response.tool_calls:
            tool_args = response.tool_calls[0]["args"]

    try:
        result = tool_entry["function"].invoke(tool_args)
    except Exception as e:
        result = {
            "summary": f"Could not connect to {tool_name}: {str(e)}",
            "dataframe": None,
            "raw": {"error": str(e)}
        }


    state["tools_called"] = [tool_name]
    state["tool_results_raw"] = [{"tool": tool_name, "args": tool_args, "result": result}]
    if result.get("dataframe") is not None:
        state["dataframes"] = [result["dataframe"]]

    current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    df_context = ""
    if result.get("dataframe") is not None and not result["dataframe"].empty:
        df_context = f"\n\nData Table (First 10 rows):\n{result['dataframe'].head(10).to_string()}"
        
    client = ChatGroq(model=SYNTHESIS_MODEL, temperature=0)
    response = client.invoke([
        {"role": "system", "content": f"You are IQCore, an IT operations assistant. Current Date: {current_date}. Answer the user's question accurately using the tool result provided. Pay strict attention to dates (e.g. today vs yesterday vs historical years like 2025). Include actual values, counts, and names."},
        {"role": "user", "content": f"Question: {state['question']}\n\nTool args used: {json.dumps(tool_args)}\n\nTool result: {result['summary']}{df_context}"},
    ])
    state["final_answer"] = response.content
    return state


