import json
from datetime import datetime, timezone
from langchain_groq import ChatGroq
from agent.state import AgentState
from agent.tool_registry import TOOL_REGISTRY
from agent.tool_schemas import TOOL_SCHEMAS
from agent.metadata import fetch_live_metadata
from agent.history import recent_turns

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

        # Follow-ups like "and what about server02?" carry no subject of their own.
        # Without the prior turns the extractor has nothing to resolve them against.
        messages = ([{"role": "system", "content": system_msg}]
                    + recent_turns(state.get("conversation_history"))
                    + [{"role": "user", "content": state["question"]}])

        # The small model occasionally emits a malformed tool call, which Groq
        # rejects with tool_use_failed. One bad generation should not fail the
        # whole request, so escalate to the larger model rather than give up.
        extracted = False
        for model in (FAST_MODEL, SYNTHESIS_MODEL):
            try:
                client = ChatGroq(model=model, temperature=0)
                response = client.bind_tools([tool_schema], tool_choice=tool_name).invoke(messages)
                if response.tool_calls:
                    tool_args = response.tool_calls[0]["args"]
                    extracted = True
                    break
            except Exception as exc:
                if "tool_use_failed" not in str(exc) and "400" not in str(exc):
                    raise      # rate limits and outages should surface, not retry
                continue

        if not extracted:
            # Falling through to the tool's defaults would silently answer a
            # different question — "what about yesterday?" would return today's
            # data and look correct. Better to say the query could not be built.
            state["final_answer"] = (
                "I could not turn that into a valid query for "
                f"{tool_name.replace('query_', '').title()}. Please rephrase it, "
                "stating the system and time period explicitly — for example "
                "'show me OpenStack error logs for yesterday'.")
            state["tools_called"] = []
            state["tool_results_raw"] = [{"tool": tool_name, "args": None,
                                          "result": {"error": "argument extraction failed"}}]
            return state

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
    response = client.invoke(
        [{"role": "system", "content": f"You are IQCore, an IT operations assistant. Current Date: {current_date}. Answer the user's question accurately using the tool result provided. Pay strict attention to dates (e.g. today vs yesterday vs historical years like 2025). Include actual values, counts, and names."}]
        + recent_turns(state.get("conversation_history"))
        + [{"role": "user", "content": f"Question: {state['question']}\n\nTool args used: {json.dumps(tool_args)}\n\nTool result: {result['summary']}{df_context}"}]
    )
    state["final_answer"] = response.content
    return state


