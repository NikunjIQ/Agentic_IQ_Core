import streamlit as st
import requests
import pandas as pd

BACKEND_URL = st.secrets.get("backend_url", "http://localhost:8000")

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
            try:
                response = requests.post(
                    f"{BACKEND_URL}/chat",
                    json={
                        "question": user_input,
                        "conversation_history": st.session_state.conversation_history,
                    },
                    timeout=180,
                )
                response.raise_for_status()
                result = response.json()

                st.markdown(result["final_answer"])

                for df_dict in result.get("dataframes", []):
                    if df_dict:
                        st.dataframe(pd.DataFrame(df_dict), use_container_width=True)

                with st.expander("Debug: agent reasoning"):
                    st.write(f"Path taken: {result.get('complexity', 'unknown')}")
                    st.write(f"Tools called: {result.get('tools_called', [])}")
                    for tr in result.get("tool_results_raw", []):
                        st.write(f"**{tr.get('tool', 'unknown')}** called with: `{tr.get('args', {})}`")
                        st.json(tr.get("result", {}))

                st.session_state.conversation_history.append({"role": "user", "content": user_input})
                st.session_state.conversation_history.append({"role": "assistant", "content": result["final_answer"]})
            except Exception as e:
                st.error(f"Error communicating with backend: {str(e)}")
