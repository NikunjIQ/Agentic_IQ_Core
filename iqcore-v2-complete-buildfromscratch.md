# IQCore v2 — Complete Build Context (From Scratch)

## Context for coding assistant

This is a **complete, standalone, from-scratch build**. Do not reuse, import, or reference any code from any prior IQCore project. There is no "Phase 1" to build first and no existing app to modify — this single document contains everything needed to build the full working system in one pass: FastAPI backend, LangGraph-based agentic reasoning with an efficiency gate, Streamlit frontend, and two working tools (Zammad, Zabbix).

**Scope, strictly:** a working chat system that answers natural language questions about Zammad tickets and Zabbix hosts/metrics, including cross-system correlation questions (e.g. "are there tickets related to hosts that are down"), with an efficiency gate so simple single-system questions don't pay the cost of full multi-step reasoning. Explicitly OUT of scope: Prometheus, OpenStack logs, Docker/generic logs, a dashboard/widgets page, Docker containerization, deployment automation, multi-thread conversation persistence, caching. Do not build placeholder files, stub code, or scaffolding for any out-of-scope item.

---

## 1. Tech stack (confirmed, do not substitute)

| Layer | Technology |
|---|---|
| Backend framework | FastAPI |
| Frontend framework | Streamlit |
| Backend-frontend communication | HTTP — Streamlit calls FastAPI as a separate process (via `requests`), NOT mounted in the same process |
| Agentic orchestration | LangGraph (a state graph, not a hand-rolled loop) |
| LLM provider | Groq, via `langchain-groq` |
| Reasoning model | `llama-3.3-70b-versatile` — used for the complex multi-step path and for final answer synthesis |
| Classification model | `llama-3.1-8b-instant` — used only for the cheap complexity-classification gate, deliberately smaller/faster than the reasoning model |
| Conversation scope | Single conversational thread per user session, no persistence across browser sessions |
| Secrets management | `.streamlit/secrets.toml` for the frontend; `.env` file loaded via `python-dotenv` for the backend |
| Containerization | None — runs as two local processes |

### 1.1 Why LangGraph specifically (context, not optional)

The system must support two different behaviors depending on question complexity: (a) simple, single-tool questions should be answered fast and cheaply, without multi-step reasoning; (b) correlation questions spanning multiple systems need iterative, multi-step tool use where each result can inform the next decision. LangGraph was chosen because it is built around explicit, inspectable, conditional control flow (a graph of nodes and edges) — this directly supports gating the expensive reasoning path behind a cheap upfront classification, which a plain hand-rolled loop would not naturally separate as cleanly.

### 1.2 Required dependencies

```
# backend/requirements.txt
fastapi
uvicorn[standard]
langgraph
langchain-groq
requests
pandas
pydantic
python-dotenv
```

```
# frontend/requirements.txt
streamlit
requests
pandas
```

Do not pin exact versions yet — install latest compatible versions first, get the system working end to end, then pin via `pip freeze` once confirmed stable. Guessing version numbers upfront risks incompatibility with LangGraph/langchain-groq's actual current API surface.

---

## 2. Project structure

```
iqcore-v2/
├── backend/
│   ├── main.py                     # FastAPI app, /chat and /health endpoints
│   ├── requirements.txt
│   ├── .env                        # secrets, gitignored — see Section 8
│   ├── agent/
│   │   ├── __init__.py
│   │   ├── state.py                # AgentState TypedDict
│   │   ├── tool_registry.py        # single source of truth for available tools
│   │   ├── tool_schemas.py         # JSON-schema tool definitions, built from the registry
│   │   ├── graph.py                # graph assembly (nodes + edges)
│   │   └── nodes/
│   │       ├── __init__.py
│   │       ├── classify.py         # cheap complexity-gate node
│   │       ├── simple_path.py      # single-tool, no-loop path
│   │       ├── complex_path.py     # multi-step tool-calling loop
│   │       └── respond.py          # final formatting pass-through
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── zammad_tool.py
│   │   └── zabbix_tool.py
│   └── models/
│       ├── __init__.py
│       └── schemas.py              # Pydantic request/response models
│
├── frontend/
│   ├── app.py                      # Streamlit chat UI
│   ├── requirements.txt
│   └── .streamlit/
│       └── secrets.toml            # gitignored
│
└── .gitignore
```

---

## 3. Graph design

```
                    ┌─────────────────────┐
                    │   classify_node       │
                    │   (cheap model call)   │
                    │   Determines:           │
                    │   - complexity:          │
                    │     "simple"/"complex"   │
                    │   - if simple: which     │
                    │     single tool to use   │
                    └──────────┬──────────────┘
                               │
                 ┌─────────────┴─────────────┐
                 │  conditional edge on         │
                 │  state["complexity"]         │
                 └──────┬───────────────┬──────┘
                         │               │
                "simple"                "complex"
                         │               │
                         v               v
              ┌────────────────┐  ┌──────────────────────┐
              │  simple_path     │  │  complex_path          │
              │  ONE tool call,  │  │  Full iterative loop:   │
              │  ONE synthesis   │  │  call tool -> see result │
              │  call. No loop.  │  │  -> decide next tool or  │
              │                  │  │  finish. Ceiling of 10.  │
              └────────┬─────────┘  └───────────┬────────────┘
                         │                        │
                         └───────────┬────────────┘
                                     v
                          ┌────────────────────┐
                          │   respond_node        │
                          │   Formats final output │
                          └────────────────────┘
```

**Design rationale for the classifier's fail-safe default:** if the cheap model's classification output is ambiguous or fails to parse cleanly, default to `"complex"`, never `"simple"`. The cost of unnecessarily taking the expensive path is wasted tokens; the cost of incorrectly taking the cheap path is a confidently wrong or incomplete answer. Correctness takes priority over token-saving when the two conflict.

**Extensibility:** this graph structure supports adding more tools (Prometheus, logs) later by adding entries to `tool_registry.py` alone — the graph itself does not need restructuring. Do not add placeholder/stub entries for out-of-scope tools now; an empty or fake registry entry would be selectable by the classifier and silently fail.

---

## 4. State schema (`backend/agent/state.py`)

```python
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
```

---

## 5. Tool registry (`backend/agent/tool_registry.py`)

The single source of truth for available tools. Both the classification node and the complex-path node read from this — never hardcode tool lists anywhere else.

```python
from tools.zammad_tool import query_zammad
from tools.zabbix_tool import query_zabbix

TOOL_REGISTRY = {
    "query_zammad": {
        "function": query_zammad,
        "description": (
            "Query the Zammad IT helpdesk and ticketing system. Use for ticket counts, "
            "states (open/closed/pending), date ranges, agent assignments, SLA/resolution data."
        ),
    },
    "query_zabbix": {
        "function": query_zabbix,
        "description": (
            "Query the Zabbix infrastructure monitoring system. Use for host counts, "
            "up/down status, availability, CPU/memory/network metrics for specific hosts."
        ),
    },
    # FUTURE: add query_prometheus, query_openstack_logs, query_logs here when that
    # phase begins. Do not add placeholder entries now.
}
```

---

## 6. Tool schemas for real tool-calling (`backend/agent/tool_schemas.py`)

Used only by the complex path (Section 9) — this is genuine LangChain/Groq tool-calling, distinct from the plain-text approach the classifier uses (Section 8). Built from the registry so descriptions never drift out of sync.

```python
from agent.tool_registry import TOOL_REGISTRY

def build_tool_schemas() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": entry["description"],
                "parameters": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "The natural language question to answer using this tool's data source."
                        }
                    },
                    "required": ["question"]
                }
            }
        }
        for name, entry in TOOL_REGISTRY.items()
    ]

TOOL_SCHEMAS = build_tool_schemas()
```

**Known limitation, acceptable for this scope:** both tools currently take only a single `question` parameter, and each tool function is responsible for interpreting that question internally (see Section 12). This works because both current tools follow this same simple shape. If a future tool needs multiple distinct structured parameters, this schema pattern and the simple-path dispatch (Section 10) will both need revisiting — not a problem for the current two tools.

---

## 7. Tool implementations

Direct, live API calls — no intermediate storage, no caching. Every question triggers a fresh call to the live system.

```python
# backend/tools/zabbix_tool.py
import os
import requests
import pandas as pd

ZABBIX_URL = os.environ["ZABBIX_URL"]
ZABBIX_HEADERS = {
    "Content-Type": "application/json-rpc",
    "Authorization": f"Bearer {os.environ['ZABBIX_API_TOKEN']}"
}

def query_zabbix(question: str) -> dict:
    """
    Fetches live host data from Zabbix. This implementation fetches ALL hosts and
    relies on the LLM's summary/reasoning step to interpret what the question actually
    needs from the full set — it does not yet do targeted, question-specific Zabbix API
    parameter construction. Acceptable for current question volume; revisit if host count
    grows large enough that fetching everything on every question becomes slow.
    """
    payload = {
        "jsonrpc": "2.0",
        "method": "host.get",
        "params": {"output": ["hostid", "host", "name", "status", "available"]},
        "id": 1
    }
    response = requests.post(ZABBIX_URL, headers=ZABBIX_HEADERS, json=payload, timeout=30)
    response.raise_for_status()
    hosts = response.json()["result"]

    df = pd.DataFrame(hosts)
    total = len(hosts)
    up = sum(1 for h in hosts if h.get("available") == "1")
    down = sum(1 for h in hosts if h.get("available") == "2")
    down_hosts = [h["host"] for h in hosts if h.get("available") == "2"]

    summary = f"Total hosts: {total}. Up: {up}. Down: {down}."
    if down_hosts:
        summary += f" Hosts currently down: {', '.join(down_hosts)}."

    return {"summary": summary, "dataframe": df, "raw": {"hosts": hosts}}
```

```python
# backend/tools/zammad_tool.py
import os
import requests
import pandas as pd

ZAMMAD_URL = os.environ["ZAMMAD_URL"]
ZAMMAD_HEADERS = {
    "Authorization": f"Token token={os.environ['ZAMMAD_API_TOKEN']}"
    # NOTE: auth method is UNVERIFIED against a real instance — see Section 13.
    # This assumes token auth as a placeholder; confirm and correct before real use.
}

def query_zammad(question: str) -> dict:
    """
    Fetches live ticket data from Zammad using a broad, unfiltered search, relying on
    the LLM's reasoning to narrow down to what the question actually asked — same
    rationale as query_zabbix. Revisit with proper server-side filtering once
    Section 13's verification is complete.
    """
    response = requests.get(
        f"{ZAMMAD_URL}/api/v1/tickets/search",
        headers=ZAMMAD_HEADERS,
        params={"query": "*"},
        timeout=30
    )
    response.raise_for_status()
    result = response.json()

    # NOTE: whether this returns a full ticket list directly, or requires a separate
    # call per ticket ID, is UNVERIFIED — see Section 13. Adjust parsing once confirmed.
    df = pd.DataFrame(result) if isinstance(result, list) else pd.DataFrame()

    summary = (
        f"Retrieved {len(df)} tickets from Zammad."
        if not df.empty
        else "No tickets found, or the Zammad API response shape did not match what was expected — verify against Section 13."
    )

    return {"summary": summary, "dataframe": df, "raw": {"result": result}}
```

---

## 8. Classification node (`backend/agent/nodes/classify.py`)

Uses plain-text prompting, NOT real tool-calling — this is a one-shot categorization task, not something that needs the heavier function-calling API. The model is asked to output a specific JSON shape as regular text, which is then parsed manually.

```python
import json
from langchain_groq import ChatGroq
from agent.state import AgentState
from agent.tool_registry import TOOL_REGISTRY

CLASSIFY_MODEL = "llama-3.1-8b-instant"

CLASSIFY_PROMPT = """You are a fast triage classifier for an IT operations assistant.
Given a user's question, decide:

1. Is this a SIMPLE question, answerable with exactly ONE call to exactly ONE tool?
   Examples: "how many hosts are down", "how many tickets are open"
2. Or is this a COMPLEX question requiring MULTIPLE tool calls and reasoning across
   more than one system? Examples: "are there tickets related to hosts that are down",
   "what's the status of host X and any related tickets"

Available tools:
{tool_descriptions}

Respond with ONLY a JSON object, no other text:
{{
  "complexity": "simple" or "complex",
  "tool": "<tool name from the list above, ONLY if complexity is simple, otherwise null>"
}}
"""

def classify_node(state: AgentState) -> AgentState:
    client = ChatGroq(model=CLASSIFY_MODEL, temperature=0)
    tool_descriptions = "\n".join(f"- {name}: {entry['description']}" for name, entry in TOOL_REGISTRY.items())
    prompt = CLASSIFY_PROMPT.format(tool_descriptions=tool_descriptions)

    response = client.invoke([
        {"role": "system", "content": prompt},
        {"role": "user", "content": state["question"]},
    ])

    try:
        parsed = json.loads(response.content)
        state["complexity"] = parsed.get("complexity", "complex")
        state["simple_tool_choice"] = parsed.get("tool")
    except (json.JSONDecodeError, AttributeError):
        # fail safe into the complex path rather than guessing — see Section 3's rationale
        state["complexity"] = "complex"
        state["simple_tool_choice"] = None

    return state
```

---

## 9. Complex path node (`backend/agent/nodes/complex_path.py`)

Real, iterative tool-calling using LangChain's `bind_tools()` — the model can genuinely decide which function to call, with what arguments, and the actual Python function executes and feeds a real result back.

```python
import json
from langchain_groq import ChatGroq
from agent.state import AgentState
from agent.tool_registry import TOOL_REGISTRY
from agent.tool_schemas import TOOL_SCHEMAS

REASONING_MODEL = "llama-3.3-70b-versatile"
MAX_TOOL_CALLS = 10

ORCHESTRATOR_SYSTEM_PROMPT = """You are IQCore, an intelligent IT operations assistant.
You have tools available to answer questions spanning multiple systems.

Rules:
- Call tools in whatever order makes sense. Call multiple tools for correlation questions.
- If a tool returns unhelpful results, try a different approach before giving up.
- When you have enough information, stop calling tools and give your final answer.
- Always include specific values from tool results — counts, hostnames, ticket IDs, states.
- If you genuinely cannot answer, say so clearly.
"""

def complex_path_node(state: AgentState) -> AgentState:
    client = ChatGroq(model=REASONING_MODEL, temperature=0)

    messages = [{"role": "system", "content": ORCHESTRATOR_SYSTEM_PROMPT}]
    messages += state["conversation_history"]
    messages.append({"role": "user", "content": state["question"]})

    tools_called = []
    tool_results_raw = []
    dataframes = []
    tool_call_count = 0
    final_answer = None

    while tool_call_count < MAX_TOOL_CALLS:
        response = client.bind_tools(TOOL_SCHEMAS).invoke(messages)

        if not response.tool_calls:
            final_answer = response.content
            break

        messages.append(response)
        for tool_call in response.tool_calls:
            tool_name = tool_call["name"]
            tool_args = tool_call["args"]
            tools_called.append(tool_name)
            tool_call_count += 1

            tool_entry = TOOL_REGISTRY.get(tool_name)
            if tool_entry is None:
                result = {"summary": f"Unknown tool: {tool_name}", "dataframe": None, "raw": None}
            else:
                try:
                    result = tool_entry["function"](**tool_args)
                except Exception as e:
                    result = {"summary": f"Tool '{tool_name}' raised an error: {str(e)}", "dataframe": None, "raw": {"error": str(e)}}

            tool_results_raw.append({"tool": tool_name, "args": tool_args, "result": result})
            if result.get("dataframe") is not None:
                dataframes.append(result["dataframe"])

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "content": json.dumps(result.get("summary", "No summary available")),
            })

    if final_answer is None:
        messages.append({"role": "user", "content": "You've reached the maximum tool calls. Summarize what you found so far."})
        response = client.invoke(messages)
        final_answer = response.content

    state["final_answer"] = final_answer
    state["tools_called"] = tools_called
    state["tool_results_raw"] = tool_results_raw
    state["dataframes"] = dataframes
    return state
```

---

## 10. Simple path node (`backend/agent/nodes/simple_path.py`)

Exactly one tool call (no schema, no function-calling — the tool name is already known from the classifier's plain-text output), followed by exactly one synthesis call. No loop possible.

```python
from langchain_groq import ChatGroq
from agent.state import AgentState
from agent.tool_registry import TOOL_REGISTRY

SYNTHESIS_MODEL = "llama-3.3-70b-versatile"

def simple_path_node(state: AgentState) -> AgentState:
    tool_name = state["simple_tool_choice"]
    tool_entry = TOOL_REGISTRY.get(tool_name)

    if tool_entry is None:
        state["final_answer"] = "I wasn't able to determine which system to check for this question. Could you rephrase it?"
        return state

    result = tool_entry["function"](question=state["question"])
    state["tools_called"] = [tool_name]
    state["tool_results_raw"] = [{"tool": tool_name, "result": result}]
    if result.get("dataframe") is not None:
        state["dataframes"] = [result["dataframe"]]

    client = ChatGroq(model=SYNTHESIS_MODEL, temperature=0)
    response = client.invoke([
        {"role": "system", "content": "You are IQCore, an IT operations assistant. Answer the user's question using the tool result provided. Be specific — include actual values, counts, and names."},
        {"role": "user", "content": f"Question: {state['question']}\n\nTool result: {result['summary']}"},
    ])
    state["final_answer"] = response.content
    return state
```

---

## 11. Respond node (`backend/agent/nodes/respond.py`)

```python
from agent.state import AgentState

def respond_node(state: AgentState) -> AgentState:
    # Currently a pass-through. Exists as a distinct node so future response
    # formatting, logging, or metrics collection has a clear single place to
    # live without touching either path's core reasoning logic.
    return state
```

---

## 12. Graph assembly (`backend/agent/graph.py`)

```python
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
```

---

## 13. FastAPI backend (`backend/main.py`)

```python
from fastapi import FastAPI
from pydantic import BaseModel
from dotenv import load_dotenv
from agent.graph import COMPILED_GRAPH

load_dotenv()

app = FastAPI(title="IQCore Agent API")

class ChatRequest(BaseModel):
    question: str
    conversation_history: list[dict] = []

class ChatResponse(BaseModel):
    final_answer: str
    dataframes: list[dict]
    tools_called: list[str]
    tool_results_raw: list[dict]
    complexity: str

@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
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
    result_state = COMPILED_GRAPH.invoke(initial_state)

    return ChatResponse(
        final_answer=result_state["final_answer"],
        dataframes=[df.to_dict(orient="records") for df in result_state["dataframes"]],
        tools_called=result_state["tools_called"],
        tool_results_raw=result_state["tool_results_raw"],
        complexity=result_state["complexity"],
    )

@app.get("/health")
async def health():
    return {"status": "ok"}
```

---

## 14. Streamlit frontend (`frontend/app.py`)

```python
import streamlit as st
import requests
import pandas as pd

BACKEND_URL = st.secrets["backend_url"]  # e.g. "http://localhost:8000"

st.set_page_config(page_title="IQCore", layout="wide")
st.title("IQCore — IT Operations Assistant")

if "conversation_history" not in st.session_state:
    st.session_state.conversation_history = []

for msg in st.session_state.conversation_history:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

if user_input := st.chat_input("Ask about tickets or infrastructure..."):
    with st.chat_message("user"):
        st.markdown(user_input)

    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            response = requests.post(
                f"{BACKEND_URL}/chat",
                json={
                    "question": user_input,
                    "conversation_history": st.session_state.conversation_history,
                },
                timeout=60,
            )
            response.raise_for_status()
            result = response.json()

        st.markdown(result["final_answer"])

        for df_dict in result["dataframes"]:
            if df_dict:
                st.dataframe(pd.DataFrame(df_dict), use_container_width=True)

        with st.expander("Debug: agent reasoning"):
            st.write(f"Path taken: {result.get('complexity', 'unknown')}")
            st.write(f"Tools called: {result['tools_called']}")
            for tr in result["tool_results_raw"]:
                st.write(f"**{tr['tool']}** called with: `{tr.get('args', {})}`")
                st.json(tr["result"])

    st.session_state.conversation_history.append({"role": "user", "content": user_input})
    st.session_state.conversation_history.append({"role": "assistant", "content": result["final_answer"]})
```

---

## 15. Environment configuration

`backend/.env` (gitignored):
```
GROQ_API_KEY=
ZABBIX_URL=http://192.168.1.152:8080/api_jsonrpc.php
ZABBIX_API_TOKEN=
ZAMMAD_URL=
ZAMMAD_API_TOKEN=
```

`frontend/.streamlit/secrets.toml` (gitignored):
```toml
backend_url = "http://localhost:8000"
```

`.gitignore`:
```
.env
.streamlit/secrets.toml
__pycache__/
*.pyc
venv/
.venv/
```

---

## 16. How to run locally

```bash
# Terminal 1 — backend
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000

# Terminal 2 — frontend
cd frontend
pip install -r requirements.txt
streamlit run app.py
```

---

## 17. Explicitly unverified — must be confirmed before production use

1. **Zammad's search endpoint behavior** — whether `/api/v1/tickets/search` returns a true full result set or requires pagination, and whether date-range query syntax works as documented. No live Zammad access was available to verify this during design.
2. **Zammad's authentication method** — token vs. basic auth vs. OAuth for the actual target instance. `zammad_tool.py` assumes token auth as an unverified placeholder.
3. **Zabbix's `available` field semantics** — `"1"` = up, `"2"` = down is assumed based on Zabbix's general API convention; spot-check against a live `host.get` call before trusting fully.

Do not silently resolve these by guessing — flag back for confirmation or test against a live instance before relying on this in front of real users.

---

## 18. Testing guidance

**Efficiency gate validation (the core new behavior to verify):**
- "how many hosts are down" → confirm `complexity: simple`, exactly one tool called
- "how many tickets are open" → confirm `complexity: simple`, exactly one tool called

**Correlation validation:**
- "are there tickets related to hosts that are down" → confirm `complexity: complex`, multiple tools called, final answer meaningfully connects both results

**Fail-safe validation:**
- A deliberately garbled/ambiguous question → confirm it falls back to the complex path rather than crashing or guessing wrong

**Conversational memory:**
- Ask a question, then a follow-up referencing it implicitly (e.g. "what about tickets for those hosts") → confirm the follow-up correctly uses conversation history

**Debug panel:**
- Every response must show the path taken, tools called, and raw tool results — this is the primary mechanism for verifying the system is behaving as designed, not just producing plausible-looking answers

---

## 19. Explicitly out of scope — do not build

Prometheus, OpenStack logs, Docker/generic logs tools, dashboard/widgets page, Docker containerization, deployment automation, multi-thread conversation persistence, caching, genuinely separate agents communicating with each other (this design uses one shared agent inside a controlled graph, not multiple independent agents passing messages).
