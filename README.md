# Agentic IQ Core — IT Operations Assistant

Agentic IQ Core is an advanced AI-powered IT Operations Assistant that provides an intelligent natural language interface over your IT infrastructure and helpdesk operations. By deeply integrating with **Zabbix** (Monitoring) and **Zammad** (Ticketing), the application uses a multi-agent LangGraph architecture to answer complex operational questions, correlate alerts with tickets, and summarize system health in real-time.

---

## 🏗 Architecture

The application is built using a modern, decoupled stack:
- **Backend**: FastAPI
- **Frontend**: Streamlit
- **AI / Orchestration**: LangChain & LangGraph
- **LLM Provider**: Groq (`llama-3.3-70b-versatile` & `llama-3.1-8b-instant`)

### Multi-Agent Orchestration (LangGraph)
The backend routes user queries through an intelligent decision graph:
1. **Classification Node**: Analyzes the query to determine complexity.
2. **Simple Path (Fast)**: For straightforward, single-tool queries (e.g., "How many tickets were updated today?"). Uses a fast, low-temperature LLM to extract arguments and execute exactly one API call.
3. **Complex Path (Reasoning)**: For queries requiring cross-system correlation (e.g., "Which down hosts in Zabbix already have open tickets in Zammad?"). An autonomous orchestrator makes multiple sequential tool calls until it gathers enough context to synthesize an answer.

### Advanced Capabilities
- **Metadata-Aware Routing (Tool-RAG)**: The backend runs a background process (`metadata.py`) that periodically fetches your live system schemas (like custom Zabbix Host Groups and Zammad Ticket States) and injects them directly into the LLMs "brain". This guarantees the AI never hallucinates API arguments and perfectly maps your natural language to your strict internal terminology.
- **DataFrame Context Injection (The "20/20 Vision" Fix)**: Instead of the LLM receiving a tiny string summary of API results, the backend converts Zabbix/Zammad JSON payloads into pandas DataFrames and injects the first 10 rows into the context window. This prevents the "Blind LLM" problem and allows the AI to accurately count, aggregate, and report specific hostnames and values.

---

## 🚀 Features

- **Zabbix Integration**: Query host statuses, availability, active problems, and specific metrics (e.g., CPU, Memory) using the Zabbix JSON-RPC API.
- **Zammad Integration**: Track SLA metrics, ticket volumes, ticket states, and agent assignments using the Zammad REST API.
- **Cross-System Correlation**: Automatically correlate a problem in the monitoring system with the corresponding ticket and assigned agent in the helpdesk.
- **Debug Trace UI**: The Streamlit frontend includes an expandable "Debug" panel that exposes the AI's internal thought process, showing exactly which path it took, which tools it called, and the raw JSON payloads it sent/received.

---

## 🛠 Setup & Installation

### 1. Prerequisites
- Python 3.10+
- Valid API Tokens for Zabbix, Zammad, and Groq.

### 2. Environment Variables
Create a `.env` file in the `backend/` directory with the following keys:
```env
# Groq API
GROQ_API_KEY=your_groq_api_key

# Zabbix Config
ZABBIX_URL=http://192.168.1.205:8080
ZABBIX_API_TOKEN=your_zabbix_token

# Zammad Config
ZAMMAD_URL=http://192.168.1.205:8082
ZAMMAD_API_TOKEN=your_zammad_token
```

### 3. Running the Backend
Navigate to the `backend` directory and start the FastAPI server:
```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

### 4. Running the Frontend
In a new terminal window, navigate to the `frontend` directory and start the Streamlit app:
```bash
cd frontend
pip install -r requirements.txt
streamlit run app.py
```

The application will be accessible at `http://localhost:8501`.

---

## 🧪 Example Complex Queries to Test

- *"List all of our hosts that are currently down in Zabbix. For each down host, check Zammad and tell me if there is already an open ticket for it."*
- *"Are there any 'Escalated' or 'Work in Progress' tickets in Zammad right now that are related to 'Routers'? Please check the CPU and Memory utilization of those specific routers in Zabbix."*
- *"Which Zabbix host group has the highest number of down devices right now? Once you find it, check Zammad to see how many 'new' tickets have been created today for that specific host group."*
