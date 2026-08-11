import os
import requests
import time
from requests.auth import HTTPBasicAuth

# Cache dictionary to store metadata and prevent spamming APIs
_METADATA_CACHE = {
    "data": None,
    "last_fetched": 0
}

CACHE_TTL_SECONDS = 300  # Cache metadata for 5 minutes

def fetch_live_metadata() -> str:
    """
    Fetches live schema data from Zabbix (hostgroups) and Zammad (ticket states).
    Returns a formatted string to inject into LLM prompts.
    Uses a 5-minute cache to reduce API load.
    """
    global _METADATA_CACHE
    current_time = time.time()
    
    if _METADATA_CACHE["data"] and (current_time - _METADATA_CACHE["last_fetched"]) < CACHE_TTL_SECONDS:
        return _METADATA_CACHE["data"]
        
    metadata_lines = ["LIVE SYSTEM SCHEMA:"]
    
    # 1. Fetch Zabbix Hostgroups
    try:
        zabbix_url = os.getenv("ZABBIX_URL", "http://192.168.1.205:8080").rstrip('/') + '/api_jsonrpc.php'
        zabbix_token = os.getenv("ZABBIX_API_TOKEN", "")
        if zabbix_token:
            headers = {'Content-Type': 'application/json-rpc', 'Authorization': f'Bearer {zabbix_token}'}
            payload = {
                "jsonrpc": "2.0",
                "method": "hostgroup.get",
                "params": {"output": ["name"]},
                "id": 1
            }
            res = requests.post(zabbix_url, headers=headers, json=payload, timeout=5)
            if res.status_code == 200:
                groups = [g.get('name') for g in res.json().get('result', []) if g.get('name')]
                if groups:
                    metadata_lines.append(f"- ZABBIX_HOST_GROUPS: {groups}")
    except Exception as e:
        metadata_lines.append(f"- ZABBIX_HOST_GROUPS: (Failed to fetch: {str(e)})")

    # 2. Fetch Zammad Ticket States
    try:
        zammad_url = os.getenv("ZAMMAD_URL", "http://192.168.1.205:8082").rstrip('/')
        zammad_token = os.getenv("ZAMMAD_API_TOKEN", "")
        zammad_user = os.getenv("ZAMMAD_USER", "")
        zammad_pass = os.getenv("ZAMMAD_PASS", "")
        
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
    except Exception as e:
        metadata_lines.append(f"- ZAMMAD_TICKET_STATES: (Failed to fetch: {str(e)})")

    # 3. Fetch Prometheus topology (node names and exporter jobs)
    try:
        from tools.clients import PrometheusClient
        prom = PrometheusClient()
        nodes = sorted(prom.node_map())
        if nodes:
            metadata_lines.append(f"- PROMETHEUS_NODES: {nodes}")
        jobs = sorted({t.get("labels", {}).get("job") for t in prom.targets()
                       if t.get("labels", {}).get("job")})
        if jobs:
            metadata_lines.append(f"- PROMETHEUS_EXPORTER_JOBS: {jobs}")
    except Exception as e:
        metadata_lines.append(f"- PROMETHEUS: (Failed to fetch: {str(e)[:80]})")

    final_schema = "\n".join(metadata_lines)
    
    # Update Cache
    _METADATA_CACHE["data"] = final_schema
    _METADATA_CACHE["last_fetched"] = current_time
    
    return final_schema

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    print(fetch_live_metadata())
