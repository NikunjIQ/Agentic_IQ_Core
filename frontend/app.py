import json
import os

import requests
import pandas as pd

import ceph_treemap
import live_graph
import streamlit as st

def _backend_url() -> str:
    """Where the API lives, in order of preference.

    st.secrets RAISES when no secrets.toml exists anywhere — the .get() default
    only covers a missing key, not a missing file. secrets.toml is gitignored, so
    a fresh clone has none and the app crashed on import before rendering
    anything. Environment variable first so a VM can point elsewhere without
    creating the file at all.
    """
    from_env = os.getenv("IQCORE_BACKEND_URL", "").strip()
    if from_env:
        return from_env
    try:
        return st.secrets.get("backend_url", "http://localhost:8000")
    except Exception:
        return "http://localhost:8000"


BACKEND_URL = _backend_url()

st.set_page_config(page_title="IQCore", layout="wide")

if "conversation_history" not in st.session_state:
    st.session_state.conversation_history = []
if "test_results" not in st.session_state:
    st.session_state.test_results = {}
if "suggestions" not in st.session_state:
    st.session_state.suggestions = []
if "pending_question" not in st.session_state:
    st.session_state.pending_question = None
if "show_live_graph" not in st.session_state:
    st.session_state.show_live_graph = True
# Assistant message index -> the payload that produced it, so earlier answers
# keep their tables when the script re-runs.
if "payloads" not in st.session_state:
    st.session_state.payloads = {}


def backend(method: str, path: str, **kwargs):
    call = requests.post if method == "post" else requests.get
    response = call(f"{BACKEND_URL}{path}", timeout=kwargs.pop("timeout", 90), **kwargs)
    response.raise_for_status()
    return response.json()


def stream_answer(question: str, history: list, result_holder: dict, status_holder,
                  graph_holder=None):
    """Yield answer text as it arrives, collecting the full payload on the side.

    st.write_stream only returns the concatenated text, so the tables and debug
    data are captured into result_holder rather than returned.

    The same stream carries node events, so when a graph_holder is supplied the
    graph is redrawn as the run moves through it.
    """
    node_states = {}
    current_tool = ""

    def redraw(note=""):
        if graph_holder is not None:
            graph_holder.markdown(
                live_graph.render(node_states, current_tool, note),
                unsafe_allow_html=True)

    redraw("starting…")

    with requests.post(f"{BACKEND_URL}/chat/stream",
                       json={"question": question, "conversation_history": history},
                       stream=True, timeout=300) as response:
        response.raise_for_status()
        for raw in response.iter_lines(decode_unicode=True):
            if not raw or not raw.startswith("data: "):
                continue
            event = json.loads(raw[6:])
            kind = event.get("type")
            if kind == "token":
                yield event.get("text", "")
            elif kind == "node":
                name, phase = event.get("node"), event.get("phase")
                node_states[name] = "active" if phase == "enter" else "done"
                redraw()
            elif kind == "tool":
                current_tool = f"running {event.get('tool', '')}"
                redraw()
            elif kind == "status":
                # Progress is where the seconds actually go — tools, not tokens.
                status_holder.caption(f"⏳ {event.get('message', '')}")
            elif kind == "done":
                status_holder.empty()
                result_holder.update(event.get("payload") or {})
                current_tool = ""
                redraw("finished")
            elif kind == "error":
                status_holder.empty()
                redraw("failed")
                yield f"\n\n{event.get('message', 'Something went wrong.')}"


def render_results(results: dict):
    for system, outcome in results.items():
        if outcome.get("ok"):
            st.success(f"**{system}** — {outcome['message']}", icon="✅")
        else:
            st.error(f"**{system}** — {outcome['message']}", icon="❌")


# ---------------------------------------------------------------- pricing
# What a token is sold for, in paise. The one number to change: every figure on
# screen is this multiplied by a token count Groq reported, so there is nothing
# else to keep in step.
#
# 2 paise/token means a 5,000-token question costs ₹100. This is a SELLING
# price, not what Groq charges — the point is to know what a query is worth
# before committing to a subscription tier.
PAISE_PER_TOKEN = 2


def rupees(tokens: int) -> str:
    """Token count as a rupee amount, e.g. 5000 -> '₹100.00'."""
    return f"₹{(tokens or 0) * PAISE_PER_TOKEN / 100:,.2f}"


def session_spend() -> tuple:
    """(answers, tokens, cost) across the whole session, from the kept payloads.

    A single query's price is too noisy to plan a tier on — a clarifying turn is
    1,400 tokens and a four-step correlation is 30,000. The running total is the
    number a subscription has to be priced against.
    """
    answered, tokens = 0, 0
    for payload in st.session_state.payloads.values():
        telemetry = (payload or {}).get("telemetry") or {}
        # A cache hit spent nothing, and billing it again would overstate the
        # session — but it was still an answer the user received.
        answered += 1
        tokens += telemetry.get("total_tokens", 0)
    return answered, tokens, rupees(tokens)


def render_payload(result: dict):
    """Everything under an answer: tables, treemap, token breakdown, debug.

    Called twice — once as the answer arrives, and again for every earlier turn
    on each rerun. Streamlit re-runs this script top to bottom on every new
    question, so anything drawn only inside the live block is gone the moment
    the next question is asked. The tables used to be exactly that.
    """
    if not result:
        return
    if result.get("cached"):
        st.caption(f"↺ cached answer ({result.get('cache_age_seconds', 0)}s old)")

    for frame in result.get("dataframes", []):
        if not frame:
            continue
        table = pd.DataFrame(frame)
        # The CRUSH hierarchy is the one result whose shape is easier to
        # see than to read: capacity split across hosts, and which OSD
        # is down. Detected by its columns rather than by tool name, so
        # any query returning that shape gets drawn.
        if {"host", "osd", "size_GB"} <= set(table.columns):
            svg = ceph_treemap.render(table.to_dict("records"))
            if svg:
                st.markdown(svg, unsafe_allow_html=True)
                st.caption(ceph_treemap.caption(table.to_dict("records")))
        st.dataframe(table, use_container_width=True)

    # Where this question's tokens went. Shown under every answer rather
    # than hidden in the debug panel: the models are capped per minute,
    # so the cost of the question just asked is operational information.
    telemetry = result.get("telemetry") or {}
    if telemetry.get("served_from_cache"):
        st.caption(
            f"🎫 0 tokens · ₹0.00 — served from cache "
            f"(cost {telemetry.get('original_total_tokens', 0):,} tokens / "
            f"{rupees(telemetry.get('original_total_tokens', 0))} when first asked)")
    elif telemetry.get("calls"):
        budget = telemetry.get("minute_budget_pct", 0)
        st.caption(
            f"🎫 {telemetry['total_tokens']:,} tokens · "
            f"**{rupees(telemetry['total_tokens'])}** · "
            f"{budget:.0f}% of a minute's allowance · "
            f"{telemetry['model_calls']} model call(s) · "
            f"{telemetry['seconds']:.1f}s"
            + ("  ⚠️ over one minute's budget" if budget > 100 else ""))

        with st.expander("Token breakdown"):
            st.markdown("**Where the tokens went**")
            st.dataframe(pd.DataFrame([
                {"Operation": e["label"], "Calls": e["calls"],
                 "Tokens": e["total_tokens"], "Cost": rupees(e["total_tokens"]),
                 "Share": f"{e['share_pct']}%"}
                for e in telemetry.get("by_operation", [])
            ]), use_container_width=True, hide_index=True)

            st.markdown("**Every call**")
            st.dataframe(pd.DataFrame([
                {"Operation": c["operation"], "Detail": c["detail"] or "—",
                 "Model": str(c["model"]).split("/")[-1],
                 "In": c["input_tokens"], "Out": c["output_tokens"],
                 "Thinking": c["reasoning_tokens"],
                 "Total": c["total_tokens"], "Seconds": c["seconds"]}
                for c in telemetry["calls"]
            ]), use_container_width=True, hide_index=True)

            reasoning = telemetry.get("reasoning_tokens", 0)
            output = telemetry.get("output_tokens", 0)
            if reasoning:
                st.caption(
                    f"{reasoning:,} of {output:,} output tokens "
                    f"({reasoning / output * 100:.0f}%) were reasoning — the "
                    f"model thinking before it answered. That work is billed "
                    f"but never appears in the reply.")
            if telemetry.get("unmeasured_calls"):
                st.warning(
                    f"{len(telemetry['unmeasured_calls'])} call(s) returned no "
                    f"usage figures, so the total above is a minimum: "
                    f"{', '.join(telemetry['unmeasured_calls'])}", icon="⚠️")
            st.caption("Figures come from Groq's own usage reporting, "
                       "not an estimate.")

    with st.expander("Debug: agent reasoning"):
        st.write(f"Path taken: {result.get('complexity', 'unknown')}")
        st.write(f"Tools called: {result.get('tools_called', [])}")
        for note in result.get("guard_notes", []):
            st.warning(f"Argument guard: {note}", icon="🛡️")
        for entry in result.get("tool_results_raw", []):
            st.write(f"**{entry.get('tool', 'unknown')}** "
                     f"called with: `{entry.get('args', {})}`")
            st.json(entry.get("result", {}))


# ---------------------------------------------------------------- connections
def connections_page():
    """Where the credentials are entered. Its own page, not the sidebar.

    Six systems with four fields each does not fit a sidebar without becoming a
    column of collapsed accordions, and it is a page you visit twice and then
    never again — which is exactly what a nav entry is for and a sidebar is not.
    """
    st.title("⚙️ Connections")
    st.caption("Credentials are stored on the server and applied without a restart.")

    try:
        config = backend("get", "/config", timeout=15)
    except Exception as exc:
        st.error(f"Cannot reach the backend at {BACKEND_URL}\n\n{exc}")
        st.stop()

    if config["configured"]:
        st.caption("Saved settings are in use. Leave a field blank to keep its current value.")
    else:
        st.warning("Not configured yet — enter your system details below.", icon="⚠️")

    entered = {}
    with st.form("credentials"):
        for group in config["groups"]:
            with st.expander(f"{group['system']} — {group['description']}",
                             expanded=not config["configured"]):
                for field in group["fields"]:
                    current = config["values"].get(field["key"], "")
                    # Secrets come back masked. Showing the mask as the value would
                    # save the bullet characters back as the password, so secrets
                    # start empty and blank means "unchanged".
                    entered[field["key"]] = st.text_input(
                        field["label"],
                        value="" if field["secret"] else current,
                        placeholder=(f"unchanged ({current})" if field["secret"] and current
                                     else field["placeholder"]),
                        type="password" if field["secret"] else "default",
                        key=f"input_{field['key']}",
                    )

        left, right = st.columns(2)
        test_clicked = left.form_submit_button("Test", use_container_width=True)
        save_clicked = right.form_submit_button("Save", type="primary",
                                                use_container_width=True)

    if test_clicked:
        with st.spinner("Checking each system…"):
            try:
                st.session_state.test_results = backend(
                    "post", "/config/test", json={"values": entered})["results"]
            except Exception as exc:
                st.error(f"Could not run the check: {exc}")

    if save_clicked:
        with st.spinner("Saving and reconnecting…"):
            try:
                result = backend("post", "/config", json={"values": entered})
                st.session_state.test_results = result.get("results", {})
                if result.get("changed"):
                    st.success(f"Saved {len(result['changed'])} setting(s). "
                               f"Cached data cleared: {', '.join(result['caches_cleared'])}.")
                else:
                    st.info("No changes to save.")
            except Exception as exc:
                st.error(f"Could not save: {exc}")

    if st.session_state.test_results:
        st.divider()
        render_results(st.session_state.test_results)

    st.divider()
    st.caption(f"Stored at `{config['path']}`")


# ---------------------------------------------------------------- chat
def chat_page():
    with st.sidebar:
        # Seeded from plain session state and written back, rather than carried by
        # a widget `key`. Streamlit drops a keyed widget's state on any run that
        # does not render it, and the Connections page does not — so a `key` here
        # silently reverted the toggle to st.toggle's own default of False every
        # time you visited Connections and came back.
        st.session_state.show_live_graph = st.toggle(
            "Show the graph while it runs",
            value=st.session_state.show_live_graph,
            help="Draws which node is executing and which tool is being called, "
                 "live, as the answer is produced.")
        if st.button("Clear chat", use_container_width=True):
            st.session_state.conversation_history = []
            st.session_state.payloads = {}
            st.rerun()

    # A fresh install has no credentials, and the form is no longer sitting in
    # front of the user, so say where it went. Silent if the backend cannot be
    # reached — the chat reports that per question, and blocking the whole page
    # on it would make an unreachable backend look like a broken app.
    try:
        if not backend("get", "/config", timeout=5)["configured"]:
            st.warning("No systems are configured yet — open **Connections** "
                       "above to add them.", icon="⚠️")
    except Exception:
        pass

    st.title("IQCore — IT Operations Assistant")

    for index, message in enumerate(st.session_state.conversation_history):
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
            # Redraw this turn's tables. Streamlit rebuilds the page from scratch on
            # every question, so an answer's tables only survive if they are drawn
            # again here from what was kept.
            render_payload(st.session_state.payloads.get(index))

    typed = st.chat_input("Ask about tickets, infrastructure, VMs or logs...")
    # A clicked suggestion is just a question the user did not have to type.
    user_input = st.session_state.pending_question or typed
    st.session_state.pending_question = None

    if user_input:
        with st.chat_message("user"):
            st.markdown(user_input)

        with st.chat_message("assistant"):
            status_holder = st.empty()
            # The graph is drawn above the answer so the route is visible while the
            # tools are still running, which is where the seconds actually go.
            graph_holder = st.empty() if st.session_state.get("show_live_graph") else None
            result = {}
            try:
                answer = st.write_stream(stream_answer(
                    user_input, st.session_state.conversation_history, result, status_holder,
                    graph_holder))
                status_holder.empty()

                render_payload(result)

                st.session_state.conversation_history += [
                    {"role": "user", "content": user_input},
                    {"role": "assistant", "content": answer or result.get("final_answer", "")},
                ]
                # Keyed by the assistant message's position, NOT stored inside the
                # message: complex_path.py splices conversation_history straight into
                # the Groq request, so a table parked in there would be sent to the
                # model as message content, against an 8k-per-minute cap.
                st.session_state.payloads[
                    len(st.session_state.conversation_history) - 1] = result
                st.session_state.suggestions = result.get("suggestions", [])
            except Exception as exc:
                status_holder.empty()
                st.session_state.suggestions = []
                st.error(f"Error communicating with backend: {exc}")

    # Follow-ups for the most recent answer. Rendered outside the message block so
    # they survive the rerun a click causes, and computed by the backend with no
    # model call — a chip costs nothing to offer.
    if st.session_state.suggestions:
        st.caption("Next:")
        columns = st.columns(len(st.session_state.suggestions))
        for column, suggestion in zip(columns, st.session_state.suggestions):
            if column.button(suggestion, key=f"chip_{hash(suggestion)}",
                             use_container_width=True):
                st.session_state.pending_question = suggestion
                st.session_state.suggestions = []
                st.rerun()


    # Drawn last, though it appears in the sidebar: `with st.sidebar` appends
    # wherever it is called, and the turn above is what writes this turn's
    # payload. Rendered with the rest of the sidebar it would show the total as
    # it stood BEFORE the answer just given, always one question behind.
    with st.sidebar:
        # One query is too noisy to price a plan on — a clarifying turn is
        # ~1,400 tokens and a four-step correlation ~30,000 — so the running
        # total and the average are the figures a tier has to be built from.
        answered, tokens, cost = session_spend()
        if answered:
            st.divider()
            st.metric("Session cost", cost,
                      help=f"{tokens:,} tokens at {PAISE_PER_TOKEN}p each")
            st.caption(f"{answered} answer(s) · avg {tokens // answered:,} tokens "
                       f"· {rupees(tokens // answered)} per query")


# Real pages rather than a session-state flag: this gives each one a URL, so
# the browser's back button and a bookmarked /connections both work.
st.navigation([
    st.Page(chat_page, title="Chat", icon="💬", default=True),
    st.Page(connections_page, title="Connections", icon="⚙️",
            url_path="connections"),
], position="top").run()
