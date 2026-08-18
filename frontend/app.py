import json
import os

import requests
import pandas as pd

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


# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("⚙️ Connection settings")

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
    st.toggle("Show the graph while it runs", key="show_live_graph",
              help="Draws which node is executing and which tool is being called, "
                   "live, as the answer is produced.")
    st.caption(f"Stored at `{config['path']}`")
    if st.button("Clear chat", use_container_width=True):
        st.session_state.conversation_history = []
        st.rerun()


# ---------------------------------------------------------------- chat
st.title("IQCore — IT Operations Assistant")

for message in st.session_state.conversation_history:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

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

            if result.get("cached"):
                st.caption(f"↺ cached answer ({result.get('cache_age_seconds', 0)}s old)")

            for frame in result.get("dataframes", []):
                if frame:
                    st.dataframe(pd.DataFrame(frame), use_container_width=True)

            with st.expander("Debug: agent reasoning"):
                st.write(f"Path taken: {result.get('complexity', 'unknown')}")
                st.write(f"Tools called: {result.get('tools_called', [])}")
                for note in result.get("guard_notes", []):
                    st.warning(f"Argument guard: {note}", icon="🛡️")
                for entry in result.get("tool_results_raw", []):
                    st.write(f"**{entry.get('tool', 'unknown')}** "
                             f"called with: `{entry.get('args', {})}`")
                    st.json(entry.get("result", {}))

            st.session_state.conversation_history += [
                {"role": "user", "content": user_input},
                {"role": "assistant", "content": answer or result.get("final_answer", "")},
            ]
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
