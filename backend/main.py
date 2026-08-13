import os
import re
import json
from typing import Any
from datetime import datetime
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv
import pandas as pd
import numpy as np

from agent.graph import COMPILED_GRAPH
from agent import answer_cache, config_store, config_test

load_dotenv()

# Disable LangSmith tracing — prevents 403 spam and reduces latency
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGCHAIN_API_KEY"] = ""

app = FastAPI(title="IQCore Agent API")

# Credentials saved through the UI take precedence over .env, so a configured
# instance keeps its settings across restarts.
_applied = config_store.apply_to_environment()

class ChatRequest(BaseModel):
    question: str
    conversation_history: list[dict] = []

class ChatResponse(BaseModel):
    final_answer: str
    dataframes: list[Any]
    tools_called: list[str]
    tool_results_raw: list[dict]
    complexity: str

def safe_serialize(obj):
    if isinstance(obj, float):
        if np.isnan(obj) or np.isinf(obj):
            return None
        return obj
    elif isinstance(obj, (pd.Timestamp, datetime)):
        return obj.isoformat()
    elif isinstance(obj, dict):
        return {str(k): safe_serialize(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple, set)):
        return [safe_serialize(v) for v in obj]
    return obj

def _explain_failure(error: Exception) -> str:
    """Turn an exception into something a user can act on.

    Without this every failure reaches the UI as a bare 500, which tells the
    person nothing — a spent LLM quota and an unreachable Zabbix look identical.
    """
    text = str(error)

    if "rate_limit_exceeded" in text or "429" in text:
        wait = re.search(r"try again in (\S+)\.\s", text)
        limit = "per-day" if "TPD" in text or "per day" in text else "per-minute"
        readable = re.sub(r"\.\d+", "", wait.group(1)) if wait else None
        return (f"The language model {limit} rate limit has been reached, so I could not "
                f"finish answering."
                + (f" Capacity returns in about {readable}." if readable else "")
                + " The monitoring and ticketing systems themselves are unaffected.")

    if "tool_use_failed" in text:
        return ("I understood the question but could not build a valid query for it. "
                "Please rephrase it slightly — naming the system or the time period "
                "explicitly usually helps.")

    if any(word in text.lower() for word in ("connection", "timeout", "unreachable", "refused")):
        return (f"I could not reach one of the backing systems (Zabbix or Zammad). "
                f"Please check they are online. Details: {text[:200]}")

    return f"Something went wrong while answering: {type(error).__name__}: {text[:300]}"


@app.post("/chat")
async def chat(request: ChatRequest):
    cached = answer_cache.get(request.question, request.conversation_history)
    if cached is not None:
        return JSONResponse(content=cached)

    initial_state = {
        "question": request.question,
        "conversation_history": request.conversation_history,
        "complexity": None,
        "simple_tool_choice": None,
        "tool_results_raw": [],
        "dataframes": [],
        "tools_called": [],
        "final_answer": None,
    }
    try:
        result_state = COMPILED_GRAPH.invoke(initial_state)
    except Exception as exc:
        # Answer with an explanation rather than a 500 the frontend cannot interpret.
        return JSONResponse(content={
            "final_answer": _explain_failure(exc),
            "dataframes": [],
            "tools_called": [],
            "tool_results_raw": [{"error": f"{type(exc).__name__}: {str(exc)[:500]}"}],
            "complexity": "error",
        })

    serialized_dfs = []
    for df in result_state.get("dataframes", []):
        if isinstance(df, pd.DataFrame):
            cleaned_df = df.where(pd.notnull(df), None)
            serialized_dfs.append(cleaned_df.to_dict(orient="records"))
        elif isinstance(df, dict):
            serialized_dfs.append(df)
        elif isinstance(df, list):
            serialized_dfs.append(df)

    payload = {
        "final_answer": result_state["final_answer"] or "",
        "dataframes": serialized_dfs,
        "tools_called": result_state["tools_called"] or [],
        "tool_results_raw": result_state["tool_results_raw"] or [],
        "complexity": result_state["complexity"] or "unknown",
    }

    try:
        clean_payload = safe_serialize(payload)
        json_str = json.dumps(clean_payload, default=str)
        serialised = json.loads(json_str)
        # Cache only what actually serialised — anything stored here is replayed
        # verbatim, so it has to be known-good JSON.
        answer_cache.put(request.question, serialised, request.conversation_history)
        return JSONResponse(content=serialised)
    except Exception as exc:
        # The answer is already computed; never lose it to a serialisation problem
        # in the debug payload.
        return JSONResponse(content={
            "final_answer": payload["final_answer"],
            "dataframes": [],
            "tools_called": payload["tools_called"],
            "tool_results_raw": [{"error": f"Could not serialise debug data: {exc}"}],
            "complexity": payload["complexity"],
        })

@app.get("/health")
async def health():
    return {"status": "ok"}


class ConfigRequest(BaseModel):
    values: dict = {}
    systems: list[str] = []


@app.get("/config")
async def get_config():
    """Current settings, secrets masked, plus the field layout for the UI."""
    return {
        "configured": config_store.is_configured(),
        "values": config_store.masked(),
        "groups": [
            {"system": system, "description": description,
             "fields": [{"key": key, "label": label, "secret": secret,
                         "placeholder": placeholder}
                        for key, label, secret, placeholder in fields]}
            for system, description, fields in config_store.FIELD_GROUPS
        ],
        "path": str(config_store.CONFIG_PATH),
    }


@app.post("/config/test")
async def test_config(request: ConfigRequest):
    """Check credentials WITHOUT saving them.

    Values already stored are merged underneath, so a user testing one changed
    field does not have to retype every masked secret.
    """
    merged = {**config_store.load(), **{k: v for k, v in request.values.items()
                                        if str(v or "").strip()}}
    return {"results": config_test.test_all(merged, request.systems or None)}


@app.post("/config")
async def save_config(request: ConfigRequest):
    """Persist credentials, apply them, and drop caches from the old ones."""
    result = config_store.save(request.values)
    merged = config_store.load()
    result["results"] = config_test.test_all(merged, request.systems or None)
    return result


@app.get("/cache")
async def cache_stats():
    """Cache hit rate — the cheapest signal for how much token spend is avoided."""
    return answer_cache.stats()


@app.post("/cache/clear")
async def cache_clear():
    answer_cache.clear()
    return {"status": "cleared"}


