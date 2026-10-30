import os
import re
import json
import threading
from datetime import datetime
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv
import pandas as pd
import numpy as np

from agent.graph import COMPILED_GRAPH
from agent import (answer_cache, auth_store, config_store, config_test, dashboard, db,
                   streaming, suggestions, telemetry, ticket_assist)

load_dotenv()

# Disable LangSmith tracing — prevents 403 spam and reduces latency
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGCHAIN_API_KEY"] = ""

app = FastAPI(title="IQCore Agent API")

# System settings (the Groq key) saved through the UI take precedence over .env.
_applied = config_store.apply_to_environment()

# Accounts, credits and per-user credentials. Creates the schema on first start
# and imports authentication.json / credentials.json once, into an empty DB.
db.init()

SESSION_COOKIE = "iqcore_session"
# Off by default because the VM serves plain http on the LAN, where a Secure
# cookie would never be sent back. Turn on the moment TLS is in front.
COOKIE_SECURE = os.getenv("IQCORE_COOKIE_SECURE", "false").lower() in ("1", "true", "yes")


class ChatRequest(BaseModel):
    question: str
    conversation_history: list[dict] = []
    conversation_id: str | None = None

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
        return (f"I could not reach one of the backing systems. "
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


# ---------------------------------------------------------------- sessions
# Every data endpoint depends on current_user. Before this the login screen was
# a curtain over the page and the API answered anyone who could reach the port;
# now a request without a live session cookie gets a 401 before it reaches any
# system or spends any credit.

def current_user(request: Request) -> dict:
    user = db.user_for_session(request.cookies.get(SESSION_COOKIE))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in first.")
    return user


def admin_user(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admins only.")
    return user


def _start_session(response: Response, user_id: int) -> None:
    response.set_cookie(SESSION_COOKIE, db.create_session(user_id),
                        max_age=db.SESSION_HOURS * 3600, path="/",
                        httponly=True, samesite="lax", secure=COOKIE_SECURE)


class AuthRequest(BaseModel):
    username: str = ""
    password: str = ""
    email: str = ""


def _public(result: dict) -> dict:
    return {k: v for k, v in result.items() if k != "user_id"}


@app.get("/auth/status")
def auth_status():
    """Whether any account exists, so a fresh install can offer sign-up first."""
    return {"accounts": db.count_users()}


@app.post("/auth/login")
def auth_login(request: AuthRequest, response: Response):
    """Sync, not async: the password hash is 200k rounds of PBKDF2, which is CPU
    work that would sit on the event loop and stall every other request."""
    result = auth_store.login(request.username, request.password)
    if result["ok"]:
        _start_session(response, result["user_id"])
    return _public(result)


@app.post("/auth/signup")
def auth_signup(request: AuthRequest, response: Response):
    """Creates the account and signs it in. Sync for the same reason as login."""
    result = auth_store.signup(request.username, request.password, request.email)
    if result["ok"]:
        _start_session(response, result["user_id"])
    return _public(result)


@app.post("/auth/logout")
def auth_logout(request: Request, response: Response):
    db.end_session(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/me")
def me(user: dict = Depends(current_user)):
    """Who is signed in, and where their credit stands."""
    balance = db.balance(user["id"])
    return {"username": user["username"], "email": user["email"], "role": user["role"],
            "allocated_paise": balance.get("allocated_paise", 0),
            "spent_paise": balance.get("spent_paise", 0),
            "remaining_paise": balance.get("remaining_paise", 0),
            "answers": balance.get("answers", 0),
            "paise_per_token": db.PAISE_PER_TOKEN}


@app.get("/dashboard/{source}")
def dashboard_source(source: str, fresh: bool = False, user: dict = Depends(current_user)):
    """One system's headline figures for the dashboard. No model call, no tokens.

    One request per system rather than one for all, so a tile fills in as soon
    as its own system answers. Sync, like /chat: it blocks on that system's API.
    """
    if source not in dashboard.SOURCES:
        raise HTTPException(status_code=404, detail="No such dashboard source.")
    return json.loads(json.dumps(safe_serialize(
        dashboard.fetch(user["id"], source, fresh)), default=str))


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/hooks/zammad/ticket-created", status_code=202)
async def zammad_ticket_created(request: Request, background: BackgroundTasks):
    """Zammad calls this when a ticket is created; IQ Core suggests a fix.

    The only endpoint that leads to a write, so it is the only one that checks
    who is calling: the body must carry Zammad's HMAC signature. It answers
    202 at once and does the work afterwards, because Zammad waits on the
    response and a model call can take longer than its webhook timeout.
    """
    raw = await request.body()
    if not ticket_assist.signature_ok(raw, request.headers.get("X-Hub-Signature", "")):
        raise HTTPException(status_code=401, detail="bad or missing signature")
    try:
        ticket_id = int(json.loads(raw)["ticket"]["id"])
    except (ValueError, KeyError, TypeError):
        raise HTTPException(status_code=400, detail="payload has no ticket id")
    background.add_task(ticket_assist.handle, ticket_id)
    return {"status": "accepted", "ticket_id": ticket_id, "mode": ticket_assist.mode()}


@app.post("/hooks/zammad/{name}/ticket-created", status_code=202)
async def zammad_connection_ticket_created(name: str, request: Request,
                                           background: BackgroundTasks):
    """The same webhook for one saved Zammad connection, checked against that
    connection's own secret, so one Zammad can never act as another. An unknown
    name gets the same 401 as a bad signature: callers learn nothing about names."""
    raw = await request.body()
    conn = db.get_zammad(name)
    if not conn or not ticket_assist.signature_ok(
            raw, request.headers.get("X-Hub-Signature", ""), conn["webhook_secret"]):
        raise HTTPException(status_code=401, detail="bad or missing signature")
    try:
        ticket_id = int(json.loads(raw)["ticket"]["id"])
    except (ValueError, KeyError, TypeError):
        raise HTTPException(status_code=400, detail="payload has no ticket id")
    background.add_task(ticket_assist.handle, ticket_id, None, conn)
    return {"status": "accepted", "ticket_id": ticket_id, "mode": conn["assist_mode"]}


@app.post("/chat/stream")
async def chat_stream(request: ChatRequest, user: dict = Depends(current_user)):
    """Same answer as /chat, delivered as it is produced.

    The graph is synchronous, so it runs in a worker thread and publishes events
    to a queue that this endpoint drains. That keeps one implementation of the
    orchestration rather than a streaming copy that would drift from it.
    """
    user_id = user["id"]
    # A label for "spent in each session": the login session this answer was
    # asked in. The hash of the token, never the token itself.
    session_id = user["token_hash"][:16]
    # Continue the conversation the page names, or start one. Only ever this
    # user's: every read filters on user_id, so a guessed id finds nothing.
    conversation_id = request.conversation_id or db.new_conversation_id()
    cached = answer_cache.get(request.question, request.conversation_history, user=user_id)

    def charge(tokens):
        # Never lose an answer to a bookkeeping failure: a charge that cannot be
        # written is a missing row, not a stalled stream.
        try:
            db.record_usage(user_id, session_id, request.question, tokens)
        except Exception:
            pass

    def remember(payload):
        # Kept so the conversation can be reopened. Same rule as a charge: a
        # failed write loses the history row, never the answer. The turn's id
        # goes back to the page so the answer can be rated.
        try:
            payload["turn_id"] = db.save_turn(user_id, conversation_id, request.question,
                                              payload.get("final_answer", ""), payload)
        except Exception:
            pass

    def events():
        if cached is not None:
            charge(0)       # free, but still an answer this user received
            done_payload = {**cached, "conversation_id": conversation_id}
            remember(done_payload)
            yield _sse({"type": "token", "text": cached.get("final_answer", "")})
            yield _sse({"type": "done", "payload": done_payload})
            return

        emitter = streaming.Emitter()

        def run():
            streaming.set_emitter(emitter)          # inside the thread it will run in
            telemetry.start()                       # same reason: contextvars are per-thread
            try:
                config_store.use_user(user_id)      # this user's credentials, and only theirs
                state = COMPILED_GRAPH.invoke({
                    "question": request.question,
                    "conversation_history": request.conversation_history,
                    "complexity": None, "simple_tool_choice": None,
                    "tool_results_raw": [], "dataframes": [],
                    "tools_called": [], "final_answer": None,
                })
                payload = _serialise_state(state)
                answer_cache.put(request.question, payload, request.conversation_history,
                                 user=user_id)
                payload = {**payload, "conversation_id": conversation_id}
                remember(payload)
                done = lambda: emitter.finish(payload)
            except Exception as exc:
                message = _explain_failure(exc)
                done = lambda: emitter.fail(message)
            # Charged BEFORE the page hears the answer is done, so the balance it
            # fetches next already includes this one. Charged on failure too: the
            # model calls that ran before it were billed by Groq all the same.
            charge((telemetry.summary() or {}).get("total_tokens", 0))
            done()

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


# ------------------------------------------------------------------ settings

class ConfigRequest(BaseModel):
    values: dict = {}
    systems: list[str] = []


@app.get("/config")
def get_config(user: dict = Depends(current_user)):
    """The caller's own settings, secrets masked, plus the field layout.

    The Groq key is a system setting, so only an admin sees or edits it.
    """
    is_admin = user["role"] == "admin"
    return {
        "configured": config_store.user_is_configured(user["id"]),
        "values": config_store.user_masked(user["id"], include_system=is_admin),
        "groups": [
            {"system": system, "description": description,
             "fields": [{"key": key, "label": label, "secret": secret,
                         "placeholder": placeholder}
                        for key, label, secret, placeholder in fields]}
            for system, description, fields in config_store.FIELD_GROUPS
            if is_admin or system not in config_store.SYSTEM_SERVICES
        ],
        "path": f"your account ({user['username']}), secrets encrypted",
    }


@app.post("/config/test")
def test_config(request: ConfigRequest, user: dict = Depends(current_user)):
    """Check credentials WITHOUT saving them.

    Sync for the same reason as /chat: test_all opens connections to five
    systems and Groq, each with its own timeout, so an unreachable host held
    the whole event loop while it waited.

    The caller's stored values are merged underneath, so testing one changed
    field does not mean retyping every masked secret.
    """
    merged = config_store.user_merged(user["id"], request.values)
    return {"results": config_test.test_all(merged, request.systems or None)}


@app.post("/config")
def save_config(request: ConfigRequest, user: dict = Depends(current_user)):
    """Save the caller's settings, drop their cached data, and test the result.

    Sync: it tests every connection before returning, same as /config/test.
    """
    result = config_store.user_save(user["id"], request.values,
                                    allow_system=user["role"] == "admin")
    result["results"] = config_test.test_all(config_store.user_merged(user["id"]),
                                             request.systems or None)
    return result


# ------------------------------------------------ conversations and pins
# Every one filtered by the signed-in user. Another user's id and a made-up one
# both answer 404: there is nothing to tell them apart by, by design.

@app.get("/conversations")
def conversations(user: dict = Depends(current_user)):
    return {"conversations": db.list_conversations(user["id"])}


@app.get("/conversations/{conversation_id}")
def conversation(conversation_id: str, user: dict = Depends(current_user)):
    turns = db.load_conversation(user["id"], conversation_id)
    if not turns:
        raise HTTPException(status_code=404, detail="No such conversation.")
    return {"id": conversation_id, "turns": turns}


@app.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: str, user: dict = Depends(current_user)):
    if not db.delete_conversation(user["id"], conversation_id):
        raise HTTPException(status_code=404, detail="No such conversation.")
    return {"ok": True}


class FeedbackRequest(BaseModel):
    turn_id: int
    rating: int


@app.post("/feedback")
def feedback(request: FeedbackRequest, user: dict = Depends(current_user)):
    """👍 (1) or 👎 (-1) on one of the caller's own answers."""
    if request.rating not in (-1, 1):
        raise HTTPException(status_code=400, detail="Rating must be 1 or -1.")
    if not db.set_feedback(user["id"], request.turn_id, request.rating):
        raise HTTPException(status_code=404, detail="No such answer.")
    return {"ok": True}


class FavouriteRequest(BaseModel):
    question: str


@app.get("/favourites")
def favourites(user: dict = Depends(current_user)):
    return {"favourites": db.list_favourites(user["id"])}


@app.post("/favourites")
def add_favourite(request: FavouriteRequest, user: dict = Depends(current_user)):
    if not request.question.strip():
        raise HTTPException(status_code=400, detail="Nothing to pin.")
    return db.add_favourite(user["id"], request.question)


@app.delete("/favourites/{favourite_id}")
def delete_favourite(favourite_id: int, user: dict = Depends(current_user)):
    if not db.delete_favourite(user["id"], favourite_id):
        raise HTTPException(status_code=404, detail="No such pin.")
    return {"ok": True}


# --------------------------------------------------------------------- admin

@app.get("/admin/feedback")
def admin_feedback(user: dict = Depends(admin_user)):
    """Every thumbs-down, with the tools that answered it — candidates for
    tests/routing_cases.py."""
    return {"downvotes": db.list_downvotes()}


class AllocationRequest(BaseModel):
    user_id: int
    allocated_rupees: float


@app.get("/admin/credits")
def admin_credits(user: dict = Depends(admin_user)):
    """Every account's allocation, spend and remaining credit."""
    return {"users": db.all_balances(), "paise_per_token": db.PAISE_PER_TOKEN}


@app.post("/admin/credits")
def admin_set_allocation(request: AllocationRequest, user: dict = Depends(admin_user)):
    if request.allocated_rupees < 0:
        raise HTTPException(status_code=400, detail="An allocation cannot be negative.")
    if not db.set_allocation(request.user_id, round(request.allocated_rupees * 100)):
        raise HTTPException(status_code=404, detail="No such account.")
    return {"ok": True, "balance": db.balance(request.user_id)}


# ---------------------------------------------------- ticket assist: Zammads
class ZammadRequest(BaseModel):
    url: str
    write_token: str = ""          # blank on an update keeps the stored token
    assist_mode: str = "dry_run"
    daily_limit: int = 40
    ansible_enabled: bool = False
    new_secret: bool = False


@app.get("/admin/zammads")
def admin_zammads(user: dict = Depends(admin_user)):
    """Every connected Zammad. Tokens and secrets never leave the server."""
    return {"zammads": db.list_zammads()}


@app.put("/admin/zammads/{name}")
def admin_save_zammad(name: str, request: ZammadRequest, user: dict = Depends(admin_user)):
    """Add or update a Zammad. The webhook secret is in the reply only when new."""
    try:
        return ticket_assist.save_connection(name, **request.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/admin/zammads/{name}")
def admin_delete_zammad(name: str, user: dict = Depends(admin_user)):
    if not db.delete_zammad(name):
        raise HTTPException(status_code=404, detail="No such Zammad.")
    return {"ok": True}


@app.post("/admin/zammads/{name}/test")
def admin_test_zammad(name: str, user: dict = Depends(admin_user)):
    """Signs in to that Zammad with the stored token, as ticket assist would."""
    conn = db.get_zammad(name)
    if not conn:
        raise HTTPException(status_code=404, detail="No such Zammad.")
    try:
        me = ticket_assist.client_for(conn).get("/api/v1/users/me") or {}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    return {"ok": True, "user": f"{me.get('firstname', '')} {me.get('lastname', '')}".strip()}



# ---------------------------------------------------------------- the UI
# Mounted LAST so every route above still wins: a mount at "/" is only reached
# once nothing else matches. Serving the page from the API process is what
# removes the second one — there is no separate frontend server, and no CORS
# because it is all one origin. That same origin is why the session cookie
# needs no cross-site configuration.
STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/")
def index():
    """index.html with the asset links stamped by their own mtime.

    Without this the browser keeps a cached app.css/app.js and an edit appears
    not to have happened — which is indistinguishable from the change being
    wrong, and cost real time before it was diagnosed. Three stat calls per page
    load is cheaper than one person reloading in confusion.
    """
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for asset in ("app.css", "app.js", "dashboard.js"):
        stamp = int((STATIC_DIR / asset).stat().st_mtime)
        html = html.replace(f"/static/{asset}", f"/static/{asset}?v={stamp}")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
