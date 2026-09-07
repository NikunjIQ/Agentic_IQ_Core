import os
import re
import json
import threading
from datetime import datetime
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from dotenv import load_dotenv
import pandas as pd
import numpy as np

from agent.graph import COMPILED_GRAPH
from agent import (answer_cache, config_store, config_test, streaming,
                   suggestions, telemetry)

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



def _serialise_state(result_state: dict) -> dict:
    """Graph state -> JSON the frontend can render."""
    serialized_dfs = []
    for df in result_state.get("dataframes", []):
        if isinstance(df, pd.DataFrame):
            cleaned_df = df.where(pd.notnull(df), None)
            serialized_dfs.append(cleaned_df.to_dict(orient="records"))
        elif isinstance(df, (dict, list)):
            serialized_dfs.append(df)

    tools_called = result_state.get("tools_called") or []
    raw = result_state.get("tool_results_raw") or []

    payload = {
        "final_answer": result_state.get("final_answer") or "",
        "dataframes": serialized_dfs,
        "tools_called": tools_called,
        "tool_results_raw": raw,
        "complexity": result_state.get("complexity") or "unknown",
        "guard_notes": result_state.get("guard_notes") or [],
        "coverage_notes": result_state.get("coverage_notes") or [],
        # Deterministic follow-ups — no model call, so these are free.
        "suggestions": suggestions.suggest(
            tools_called, suggestions.looks_empty(serialized_dfs, raw)),
        # Where this question's tokens went, straight from Groq's usage figures.
        "telemetry": telemetry.summary(),
    }

    try:
        return json.loads(json.dumps(safe_serialize(payload), default=str))
    except Exception as exc:
        # The answer is already computed; never lose it to a serialisation problem
        # in the debug payload.
        return {
            "final_answer": payload["final_answer"],
            "dataframes": [],
            "tools_called": payload["tools_called"],
            "tool_results_raw": [{"error": f"Could not serialise debug data: {exc}"}],
            "complexity": payload["complexity"],
            "guard_notes": payload["guard_notes"],
            "coverage_notes": payload["coverage_notes"],
            "suggestions": payload["suggestions"],
        }

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    """Same answer as /chat, delivered as it is produced.

    The graph is synchronous, so it runs in a worker thread and publishes events
    to a queue that this endpoint drains. That keeps one implementation of the
    orchestration rather than a streaming copy that would drift from it.
    """
    cached = answer_cache.get(request.question, request.conversation_history)

    def events():
        if cached is not None:
            yield _sse({"type": "token", "text": cached.get("final_answer", "")})
            yield _sse({"type": "done", "payload": cached})
            return

        emitter = streaming.Emitter()

        def run():
            streaming.set_emitter(emitter)          # inside the thread it will run in
            telemetry.start()                       # same reason: contextvars are per-thread
            try:
                state = COMPILED_GRAPH.invoke({
                    "question": request.question,
                    "conversation_history": request.conversation_history,
                    "complexity": None, "simple_tool_choice": None,
                    "tool_results_raw": [], "dataframes": [],
                    "tools_called": [], "final_answer": None,
                })
                payload = _serialise_state(state)
                answer_cache.put(request.question, payload, request.conversation_history)
                emitter.finish(payload)
            except Exception as exc:
                emitter.fail(_explain_failure(exc))

        threading.Thread(target=run, daemon=True).start()

        while True:
            event = emitter.events.get()
            if event is streaming.DONE:
                break
            yield _sse(event)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


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
def test_config(request: ConfigRequest):
    """Check credentials WITHOUT saving them.

    Sync for the same reason as /chat: test_all opens connections to five
    systems and Groq, each with its own timeout, so an unreachable host held
    the whole event loop while it waited.

    Values already stored are merged underneath, so a user testing one changed
    field does not have to retype every masked secret.
    """
    merged = {**config_store.load(), **{k: v for k, v in request.values.items()
                                        if str(v or "").strip()}}
    return {"results": config_test.test_all(merged, request.systems or None)}


@app.post("/config")
def save_config(request: ConfigRequest):
    """Persist credentials, apply them, and drop caches from the old ones.

    Sync: it tests every connection before returning, same as /config/test.
    """
    result = config_store.save(request.values)
    merged = config_store.load()
    result["results"] = config_test.test_all(merged, request.systems or None)
    return result



