import requests
from requests.auth import HTTPBasicAuth

from agent import config_store
from tools.clients import Cached

CACHE_TTL_SECONDS = 300  # Cache metadata for 5 minutes

# The schema prose and the same fetch kept as values, under one timestamp:
# clarifying questions must offer real host groups and states, so they need the
# list, not the sentence. Holding both together is what stops the two halves
# expiring separately.
_METADATA_CACHE = Cached("live schema", CACHE_TTL_SECONDS)

def fetch_live_metadata() -> str:
    """
    Fetches live schema data from Zabbix (hostgroups) and Zammad (ticket states).
    Returns a formatted string to inject into LLM prompts.
    Uses a 5-minute cache to reduce API load.
    """
    cached = _METADATA_CACHE.get()
    if cached:
        return cached["schema"]

    try:
        from agent.config_store import ensure_applied
        ensure_applied()
    except Exception:
        pass

    metadata_lines = ["LIVE SYSTEM SCHEMA:"]
    options = {}
    
    # 1. Fetch Zabbix Hostgroups
    try:
        if config_store.get("ZABBIX_USER", "") and config_store.get("ZABBIX_PASS", ""):
            from tools.zabbix.client import build_rpc
            groups = [g.get('name') for g in build_rpc()("hostgroup.get", {"output": ["name"]})
                      if g.get('name')]
            if groups:
                metadata_lines.append(f"- ZABBIX_HOST_GROUPS: {groups}")
                options["zabbix_groups"] = groups
    except Exception as e:
        metadata_lines.append(f"- ZABBIX_HOST_GROUPS: (Failed to fetch: {str(e)})")

    # 2. Fetch Zammad Ticket States
    try:
        zammad_url = config_store.get("ZAMMAD_URL", "http://192.168.1.205:8082").rstrip('/')
        zammad_token = config_store.get("ZAMMAD_API_TOKEN", "")
        zammad_user = config_store.get("ZAMMAD_USER", "")
        zammad_pass = config_store.get("ZAMMAD_PASS", "")
        
        headers = {}
        auth = None
        if zammad_token:
            headers["Authorization"] = f"Token token={zammad_token}"
        elif zammad_user and zammad_pass:
            auth = HTTPBasicAuth(zammad_user, zammad_pass)
            
        if headers or auth:
            res = requests.get(f"{zammad_url}/api/v1/ticket_states", headers=headers, auth=auth, timeout=5)
            if res.status_code == 200:
                states = [s.get('name') for s in res.json() if s.get('name')]
                if states:
                    metadata_lines.append(f"- ZAMMAD_TICKET_STATES: {states}")
                    options["zammad_states"] = states
    except Exception as e:
        metadata_lines.append(f"- ZAMMAD_TICKET_STATES: (Failed to fetch: {str(e)})")

    # 3. Fetch Prometheus topology (node names and exporter jobs)
    try:
        from tools.clients import PrometheusClient
        prom = PrometheusClient()
        nodes = sorted(prom.node_map())
        if nodes:
            metadata_lines.append(f"- PROMETHEUS_NODES: {nodes}")
            options["prometheus_nodes"] = nodes
        jobs = sorted({t.get("labels", {}).get("job") for t in prom.targets()
                       if t.get("labels", {}).get("job")})
        if jobs:
            metadata_lines.append(f"- PROMETHEUS_EXPORTER_JOBS: {jobs}")
    except Exception as e:
        metadata_lines.append(f"- PROMETHEUS: (Failed to fetch: {str(e)[:80]})")

    final_schema = "\n".join(metadata_lines)
    _METADATA_CACHE.put({"schema": final_schema, "options": options})
    return final_schema


def live_options() -> dict:
    """Real values from the connected systems, for building clarifying questions.

    Shares the 5-minute cache with fetch_live_metadata, so asking the user a
    question costs no extra API calls.
    """
    cached = _METADATA_CACHE.get()
    if not cached:
        fetch_live_metadata()
        cached = _METADATA_CACHE.get() or {}
    return dict(cached.get("options") or {})

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    print(fetch_live_metadata())
