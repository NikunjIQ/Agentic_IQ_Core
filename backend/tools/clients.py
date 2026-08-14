"""
Thin API clients for Zabbix and Zammad.

The @tool functions each grew their own copy of auth + request plumbing. The
correlation engine needs the same plumbing without going through the tool
wrappers, so it lives here once.
"""

import os
import time
import concurrent.futures
from typing import Optional

import requests
from requests.auth import HTTPBasicAuth


def _ensure_config():
    """Merge credentials saved through the UI before reading the environment.

    Clients are built per call and read os.getenv at that moment, so doing this
    here covers every entry point rather than only the FastAPI app.
    """
    try:
        from agent.config_store import ensure_applied
        ensure_applied()
    except Exception:
        pass

# Zabbix severity codes -> labels (matches the Zabbix UI)
SEVERITY_LABELS = {
    0: "Not classified",
    1: "Information",
    2: "Warning",
    3: "Average",
    4: "High",
    5: "Disaster",
}

# Tag names the Zabbix->Zammad webhook writes back onto the event.
TICKET_ID_TAG = "__zbx_zammad_ticket_id"
TICKET_LINK_TAG = "__zbx_zammad_ticketlink"

# Pulling the whole ticket estate takes 12-30s. A question that needs two cuts of
# the same data should not pay for it twice.
#
# 60s is deliberately short for the running app: this is monitoring data and an
# answer should not be built on a stale estate. The eval suite raises it via
# ZAMMAD_CACHE_TTL, because a dozen Zammad checks in one run would otherwise
# re-download 3,788 tickets four or five times. That is a test concern only —
# changing the default would trade the app's freshness for speed, which is a
# separate decision.
_TICKET_CACHE = {"tickets": None, "fetched_at": 0.0}
TICKET_CACHE_TTL = int(os.getenv("ZAMMAD_CACHE_TTL", "60"))


def _ticket_cache_get():
    import time
    if (_TICKET_CACHE["tickets"] is not None
            and time.time() - _TICKET_CACHE["fetched_at"] < TICKET_CACHE_TTL):
        return _TICKET_CACHE["tickets"]
    return None


def _ticket_cache_put(tickets):
    import time
    _TICKET_CACHE["tickets"] = tickets
    _TICKET_CACHE["fetched_at"] = time.time()


# ---------------------------------------------------------------------------
# Zabbix
# ---------------------------------------------------------------------------
class ZabbixClient:
    def __init__(self):
        _ensure_config()
        url = os.getenv("ZABBIX_URL", "http://192.168.1.205:8080").rstrip("/")
        if not url.endswith("api_jsonrpc.php"):
            url += "/api_jsonrpc.php"
        self.url = url
        self.headers = {"Content-Type": "application/json-rpc"}
        self._auth = None

        token = os.getenv("ZABBIX_API_TOKEN", "")
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        else:
            user, password = os.getenv("ZABBIX_USER", ""), os.getenv("ZABBIX_PASS", "")
            if user and password:
                # 6.4+ renamed the login param from "user" to "username"
                for key in ("username", "user"):
                    try:
                        res = requests.post(self.url, headers=self.headers, timeout=15, json={
                            "jsonrpc": "2.0", "method": "user.login",
                            "params": {key: user, "password": password}, "id": 1})
                        data = res.json()
                        if "result" in data:
                            self._auth = data["result"]
                            break
                    except Exception:
                        continue

    def call(self, method: str, params: dict) -> list:
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 2}
        if self._auth:
            payload["auth"] = self._auth
        res = requests.post(self.url, headers=self.headers, json=payload, timeout=30)
        data = res.json()
        if "error" in data:
            raise RuntimeError(data["error"].get("data") or data["error"])
        return data.get("result", [])

    # -- convenience wrappers -------------------------------------------------

    def hosts(self) -> list:
        return self.call("host.get", {
            "output": ["hostid", "host", "name", "status", "active_available"],
            "selectInterfaces": ["type", "ip", "dns", "available", "error"],
            "selectHostGroups": ["name", "groupid"],
        })

    def problems(self, limit: int = 500) -> list:
        """Active problems with the tags that carry the Zammad ticket id."""
        return self.call("problem.get", {
            "output": ["eventid", "objectid", "clock", "name", "severity", "acknowledged"],
            "selectTags": "extend",
            "recent": True,
            "sortfield": ["eventid"], "sortorder": "DESC",
            "limit": limit,
        })

    def triggers_to_hosts(self, trigger_ids: list) -> dict:
        """triggerid -> [host dicts]. One call for the whole batch."""
        if not trigger_ids:
            return {}
        trigs = self.call("trigger.get", {
            "triggerids": list({str(t) for t in trigger_ids}),
            "output": ["triggerid", "description"],
            "selectHosts": ["hostid", "host", "name"],
        })
        return {t["triggerid"]: t.get("hosts", []) for t in trigs}

    def events_with_ticket_tags(self, limit: int = 1000) -> dict:
        """ticket_id -> event. Builds the ticket->device reverse index."""
        events = self.call("event.get", {
            "output": ["eventid", "objectid", "clock", "name", "severity"],
            "selectTags": "extend", "source": 0, "object": 0,
            "sortfield": ["eventid"], "sortorder": "DESC", "limit": limit,
        })
        index = {}
        for ev in events:
            for tag in ev.get("tags") or []:
                if tag.get("tag") == TICKET_ID_TAG and tag.get("value"):
                    index.setdefault(str(tag["value"]), ev)
        return index


# ---------------------------------------------------------------------------
# Prometheus
# ---------------------------------------------------------------------------
_PROM_NODE_CACHE = {"map": None, "fetched_at": 0.0}
PROM_NODE_CACHE_TTL = 300


class PrometheusClient:
    def __init__(self):
        _ensure_config()
        self.url = os.getenv("PROMETHEUS_URL", "http://192.168.1.29:9190").rstrip("/")
        user = os.getenv("PROMETHEUS_USER", "")
        password = os.getenv("PROMETHEUS_PASS", "")
        self.auth = HTTPBasicAuth(user, password) if user else None
        self.headers = {}
        token = os.getenv("PROMETHEUS_TOKEN", "")
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def api(self, path: str, params: Optional[dict] = None):
        res = requests.get(f"{self.url}/api/v1/{path}", params=params or {},
                           headers=self.headers, auth=self.auth, timeout=30)
        res.raise_for_status()
        body = res.json()
        if body.get("status") != "success":
            raise RuntimeError(body.get("error", "Prometheus returned a non-success status"))
        return body.get("data")

    def instant(self, promql: str) -> list:
        return (self.api("query", {"query": promql}) or {}).get("result", [])

    def over_time(self, promql: str, minutes: int = 60, step: str = "1m") -> list:
        end = int(time.time())
        return (self.api("query_range", {"query": promql, "start": end - minutes * 60,
                                         "end": end, "step": step}) or {}).get("result", [])

    def targets(self) -> list:
        return (self.api("targets") or {}).get("activeTargets", [])

    def alerts(self) -> list:
        return (self.api("alerts") or {}).get("alerts", [])

    def rules(self) -> list:
        return (self.api("rules") or {}).get("groups", [])

    def metric_names(self) -> list:
        return self.api("label/__name__/values") or []

    def label_values(self, label: str) -> list:
        return self.api(f"label/{label}/values") or []

    def node_map(self) -> dict:
        """Friendly hostname -> instance address, from node_uname_info.

        People ask about "server02", not "192.168.1.30:9100". Without this the
        host filter can only match raw addresses.
        """
        now = time.time()
        if (_PROM_NODE_CACHE["map"] is not None
                and now - _PROM_NODE_CACHE["fetched_at"] < PROM_NODE_CACHE_TTL):
            return _PROM_NODE_CACHE["map"]

        mapping = {}
        try:
            for series in self.instant("node_uname_info"):
                labels = series.get("metric", {})
                name, instance = labels.get("nodename"), labels.get("instance")
                if name and instance:
                    mapping[name] = instance
        except Exception:
            pass

        _PROM_NODE_CACHE["map"] = mapping
        _PROM_NODE_CACHE["fetched_at"] = now
        return mapping


# ---------------------------------------------------------------------------
# OpenSearch (OpenStack application logs, shipped by fluentd)
# ---------------------------------------------------------------------------
# The same severity is written many ways: 'error' appears 1,075,935 times and
# 'ERROR' 23,657. Counting one spelling answers "how many errors today" wrong by
# a factor of 45, so every level is folded into a canonical bucket.
LOG_LEVEL_ALIASES = {
    "critical": ["CRITICAL", "critical", "EMER", "emerg", "alert", "FATAL", "fatal"],
    "error": ["ERROR", "error", "Error", "ERR", "err"],
    "warning": ["WARNING", "warning", "Warning", "WARN", "warn"],
    "info": ["INFO", "info", "Info", "Note", "note", "notice", "NOTICE"],
    "debug": ["DEBUG", "debug", "Debug", "TRACE", "trace"],
}
_LEVEL_OF_VARIANT = {v: canonical
                     for canonical, variants in LOG_LEVEL_ALIASES.items()
                     for v in variants}


def canonical_level(raw: str) -> str:
    return _LEVEL_OF_VARIANT.get(str(raw), str(raw).lower() or "unknown")


class OpenSearchClient:
    def __init__(self):
        _ensure_config()
        self.url = os.getenv("OPENSEARCH_URL", "http://192.168.1.32:9200").rstrip("/")
        self.index = os.getenv("OPENSEARCH_INDEX", "flog-*")
        user = os.getenv("OPENSEARCH_USER", "")
        password = os.getenv("OPENSEARCH_PASS", "")
        self.auth = HTTPBasicAuth(user, password) if user else None

    def search(self, body: dict, path: str = "_search") -> dict:
        res = requests.post(f"{self.url}/{self.index}/{path}", json=body,
                            auth=self.auth, timeout=45)
        res.raise_for_status()
        return res.json()

    def count(self, query: dict) -> int:
        res = requests.post(f"{self.url}/{self.index}/_count", json={"query": query},
                            auth=self.auth, timeout=45)
        res.raise_for_status()
        return int(res.json().get("count", 0))

    def indices(self) -> list:
        try:
            res = requests.get(f"{self.url}/_cat/indices/{self.index}",
                               params={"format": "json", "h": "index,docs.count",
                                       "s": "index:desc"},
                               auth=self.auth, timeout=30)
            res.raise_for_status()
            return res.json()
        except Exception:
            return []


# ---------------------------------------------------------------------------
# Zammad
# ---------------------------------------------------------------------------
class ZammadClient:
    def __init__(self):
        _ensure_config()
        self.url = os.getenv("ZAMMAD_URL", "http://192.168.1.205:8082").rstrip("/")
        self.headers = {}
        self.auth = None

        token = os.getenv("ZAMMAD_API_TOKEN", "")
        if token:
            self.headers["Authorization"] = f"Token token={token}"
        else:
            user, password = os.getenv("ZAMMAD_USER", ""), os.getenv("ZAMMAD_PASS", "")
            if user and password:
                self.auth = HTTPBasicAuth(user, password)

    def get(self, path: str, params: Optional[dict] = None):
        res = requests.get(f"{self.url}{path}", headers=self.headers,
                           auth=self.auth, params=params or {}, timeout=25)
        if res.status_code == 404:
            return None
        res.raise_for_status()
        return res.json()

    def ticket(self, ticket_id) -> Optional[dict]:
        try:
            return self.get(f"/api/v1/tickets/{ticket_id}")
        except Exception:
            return None

    def tickets_by_id(self, ticket_ids: list) -> dict:
        """Fetch exactly the tickets we care about, concurrently.

        This is the whole point of the event-tag join: we already know which
        ticket ids matter, so there is no need to page through the estate.
        """
        ids = [str(t) for t in ticket_ids if t]
        if not ids:
            return {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            fetched = list(pool.map(self.ticket, ids))
        return {tid: t for tid, t in zip(ids, fetched) if t}

    def articles(self, ticket_id) -> list:
        try:
            return self.get(f"/api/v1/ticket_articles/by_ticket/{ticket_id}") or []
        except Exception:
            return []

    def state_map(self) -> dict:
        try:
            return {int(s["id"]): s.get("name", str(s["id"]))
                    for s in (self.get("/api/v1/ticket_states") or []) if "id" in s}
        except Exception:
            return {}

    def priority_map(self) -> dict:
        try:
            return {int(p["id"]): p.get("name", str(p["id"]))
                    for p in (self.get("/api/v1/ticket_priorities") or []) if "id" in p}
        except Exception:
            return {}

    def organization_map(self) -> dict:
        try:
            return {int(o["id"]): o.get("name", str(o["id"]))
                    for o in (self.get("/api/v1/organizations", {"per_page": 200}) or [])
                    if "id" in o}
        except Exception:
            return {}

    def group_map(self) -> dict:
        try:
            return {int(g["id"]): g.get("name", str(g["id"]))
                    for g in (self.get("/api/v1/groups") or []) if "id" in g}
        except Exception:
            return {}

    def tag_counts(self) -> list:
        """Tag names with ticket counts, straight from the admin endpoint.

        Tags are not on the ticket object, and asking per ticket would be one
        request each. This answers "which tag has the most tickets" in one call.
        """
        try:
            tags = self.get("/api/v1/tag_list") or []
            return sorted(
                [{"tag": t.get("name"), "tickets": int(t.get("count") or 0)} for t in tags],
                key=lambda t: t["tickets"], reverse=True)
        except Exception:
            return []

    def all_tickets(self, page_size: int = 100, max_pages: int = 200) -> list:
        """Every ticket, paging until the API stops returning rows.

        The previous fixed range of 44 pages was already within 7 pages of this
        estate's size; past that it would have dropped tickets with no error.
        """
        cached = _ticket_cache_get()
        if cached is not None:
            return cached

        tickets, page = [], 1
        while page <= max_pages:
            batch = self.get("/api/v1/tickets", {"page": page, "per_page": page_size})
            if not batch:
                break
            tickets.extend(batch)
            if len(batch) < page_size:
                break
            page += 1

        if not tickets:  # search fallback, mirrors the original behaviour
            tickets = self.search("*", limit=1000)

        _ticket_cache_put(tickets)
        return tickets

    def search(self, query: str, limit: int = 50) -> list:
        """Full-text ticket search — used for human-raised tickets that carry
        no Zabbix event id."""
        try:
            data = self.get("/api/v1/tickets/search", {"query": query, "limit": limit})
        except Exception:
            return []
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            assets = data.get("assets", {}).get("Ticket", {})
            return list(assets.values())
        return []
