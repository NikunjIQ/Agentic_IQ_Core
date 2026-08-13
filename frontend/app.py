import requests
import pandas as pd
import streamlit as st

BACKEND_URL = st.secrets.get("backend_url", "http://localhost:8000")

st.set_page_config(page_title="IQCore", layout="wide")

if "conversation_history" not in st.session_state:
    st.session_state.conversation_history = []
if "test_results" not in st.session_state:
    st.session_state.test_results = {}


def backend(method: str, path: str, **kwargs):
    call = requests.post if method == "post" else requests.get
    response = call(f"{BACKEND_URL}{path}", timeout=kwargs.pop("timeout", 90), **kwargs)
    response.raise_for_status()
    return response.json()


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
    st.caption(f"Stored at `{config['path']}`")
    if st.button("Clear chat", use_container_width=True):
        st.session_state.conversation_history = []
        st.rerun()


# ---------------------------------------------------------------- chat
st.title("IQCore — IT Operations Assistant")

for message in st.session_state.conversation_history:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

if user_input := st.chat_input("Ask about tickets, infrastructure, VMs or logs..."):
    with st.chat_message("user"):
        st.markdown(user_input)

    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            try:
                result = backend("post", "/chat", timeout=180, json={
                    "question": user_input,
                    "conversation_history": st.session_state.conversation_history,
                })

                st.markdown(result["final_answer"])
                if result.get("cached"):
                    st.caption(f"↺ cached answer ({result.get('cache_age_seconds', 0)}s old)")

                for frame in result.get("dataframes", []):
                    if frame:
                        st.dataframe(pd.DataFrame(frame), use_container_width=True)

                with st.expander("Debug: agent reasoning"):
                    st.write(f"Path taken: {result.get('complexity', 'unknown')}")
                    st.write(f"Tools called: {result.get('tools_called', [])}")
                    for entry in result.get("tool_results_raw", []):
                        st.write(f"**{entry.get('tool', 'unknown')}** "
                                 f"called with: `{entry.get('args', {})}`")
                        st.json(entry.get("result", {}))

                st.session_state.conversation_history += [
                    {"role": "user", "content": user_input},
                    {"role": "assistant", "content": result["final_answer"]},
                ]
            except Exception as exc:
                st.error(f"Error communicating with backend: {exc}")
