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


# One extra attempt by default. Set IQCORE_HTTP_RETRIES=0 to turn it off, which
# is what the evals do when they want a failure to stay a failure.
def _retry_budget() -> int:
    try:
        return max(0, int(os.getenv("IQCORE_HTTP_RETRIES", "1")))
    except ValueError:
        return 1


# Only these get a second attempt. Everything else is the server answering —
# it just did not answer the way we wanted, and asking again changes nothing.
_WORTH_RETRYING = (requests.exceptions.Timeout,
                   requests.exceptions.ConnectionError)


def request(method: str, url: str, *, system: str = "", **kwargs):
    """One HTTP request, tried again if the connection was the problem.

    Ceph's image listing answered the same call in 45 seconds (timed out), then
    25, then 10.5, with nothing changed. A single attempt turned that into
    "could not connect to Ceph" after a 45-second wait — and the question had
    already spent a full round of model calls by then, so the user paid twice:
    once for the failed answer, once for retyping it.

    Deliberately NOT a LangGraph RetryPolicy on the node. That would never fire,
    because both paths catch tool failures inside the node and return normally
    rather than raising. And if it did fire it would re-run the whole node,
    including the argument-extraction model call — spending tokens because a
    socket hiccuped. Retrying here repeats only the request that failed.

    What is not retried matters as much as what is. An HTTP error arrives as
    HTTPError from raise_for_status, never as Timeout, so 401, 404, 429 and 500
    all fall through untouched. 429 especially: the models are capped per minute
    AND per day, and retrying a rate limit spends the remainder faster while
    never succeeding.
    """
    attempts = _retry_budget() + 1
    last = None
    for attempt in range(attempts):
        try:
            return requests.request(method, url, **kwargs)
        except _WORTH_RETRYING as exc:
            last = exc
            if attempt + 1 >= attempts:
                break
            # Say so rather than just taking twice as long. Imported here, not
            # at module level: tools/ does not depend on agent/, and the one
            # existing exception (_ensure_config above) defers the same way.
            try:
                from agent import streaming
                streaming.status(f"{system or 'The system'} did not respond, "
                                 f"trying once more…")
            except Exception:
                pass
    raise last

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
                        res = request(
                            "POST", self.url, system="Zabbix",
                            headers=self.headers, timeout=15,
                            json={"jsonrpc": "2.0", "method": "user.login",
                                  "params": {key: user, "password": password},
                                  "id": 1})
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
        res = request("POST", self.url, system="Zabbix",
                      headers=self.headers, json=payload, timeout=30)
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
        res = request("GET", f"{self.url}/api/v1/{path}", system="Prometheus",
                      params=params or {},
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
        res = request("POST", f"{self.url}/{self.index}/{path}", system="OpenSearch",
                      json=body,
                            auth=self.auth, timeout=45)
        res.raise_for_status()
        return res.json()

    def count(self, query: dict) -> int:
        res = request("POST", f"{self.url}/{self.index}/_count", system="OpenSearch",
                      json={"query": query},
                            auth=self.auth, timeout=45)
        res.raise_for_status()
        return int(res.json().get("count", 0))

    def indices(self) -> list:
        try:
            res = request("GET", f"{self.url}/_cat/indices/{self.index}", system="OpenSearch",
                               params={"format": "json", "h": "index,docs.count",
                                       "s": "index:desc"},
                               auth=self.auth, timeout=30)
            res.raise_for_status()
            return res.json()
        except Exception:
            return []


# ---------------------------------------------------------------------------
# Second OpenSearch cluster — container logs shipped from server01
# ---------------------------------------------------------------------------
class CephDaemonLogClient:
    """The podman index: stdout from Ceph's containerised daemons.

    A separate cluster from OpenSearchClient above, on its own host with its
    own credentials, and HTTPS with a self-signed certificate — plain HTTP is
    refused at the socket in a way that reads like the host being down rather
    than a scheme mismatch, which cost real time to diagnose.

    Deliberately NOT called a log client for Ceph itself. This index holds what
    Ceph's containers printed, not what the Ceph cluster logged: no OSD events,
    no PG state, no health checks. Those are CephClient's /api/logs. Probing
    found HEALTH_OK, slow ops, pg, mon and mgr at zero occurrences here.
    """

    HEARTBEAT = "system:0"      # 83% of the Ceph rows; carries no information

    def __init__(self):
        _ensure_config()
        raw = os.getenv("OPENSEARCH2_URL", "https://192.168.1.152:9200").rstrip("/")
        if not raw.startswith("http"):
            raw = "https://" + raw
        self.url = raw
        self.index = os.getenv("OPENSEARCH2_CEPH_INDEX", "podman-20*")
        user = os.getenv("OPENSEARCH2_USER", "")
        password = os.getenv("OPENSEARCH2_PASS", "")
        self.auth = HTTPBasicAuth(user, password) if user else None
        self.verify = str(os.getenv("OPENSEARCH2_VERIFY_TLS", "false")).lower() in (
            "1", "true", "yes")
        if not self.verify:
            # Same reason as CephClient: a self-signed certificate would
            # otherwise print a warning on every single request.
            try:
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            except Exception:
                pass

    @property
    def configured(self) -> bool:
        return bool(self.url and self.auth)

    def search(self, body: dict) -> dict:
        # track_total_hits, always. OpenSearch stops counting at 10,000 by
        # default and reports exactly that, so a 61,000-document index answers
        # "10000" to any count — a wrong number with no error attached, which
        # is the silent-truncation trap three other systems here already have.
        body = {"track_total_hits": True, **body}
        res = request("POST", f"{self.url}/{self.index}/_search",
                      system="Ceph daemon logs", json=body,
                      auth=self.auth, verify=self.verify, timeout=45)
        res.raise_for_status()
        return res.json()

    def coverage(self) -> dict:
        """Earliest and latest document, so an answer can state what it saw.

        The index is daily-rotated and was six hours deep when this was
        written. A question about last week finds nothing, and without this
        that reads as "nothing happened" rather than "not collected yet".
        """
        try:
            data = self.search({"size": 0, "aggs": {
                "first": {"min": {"field": "@timestamp"}},
                "last": {"max": {"field": "@timestamp"}}}})
            aggs = data.get("aggregations", {})
            return {"first": aggs.get("first", {}).get("value_as_string"),
                    "last": aggs.get("last", {}).get("value_as_string"),
                    "documents": data.get("hits", {}).get("total", {}).get("value", 0)}
        except Exception:
            return {}


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
        res = request("GET", f"{self.url}{path}", system="Zammad", headers=self.headers,
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

    # ---- families the docs list and the tool never used -------------------
    # Probed against Zammad 6.5.2: SLAs (2), time accounting (32 entries across
    # 16 tickets), overviews (12), tags (110). Ticket links, checklists,
    # knowledge base and templates all came back empty, so nothing reads them.

    # Zammad's ticket search returns at most this many rows whatever `limit`
    # asks for — 200, 500, 1000 and 2000 all come back with 200. A caller that
    # trusts the row count silently under-reports any tag with more tickets
    # than this, so the cap is named and checked rather than discovered.
    SEARCH_CAP = 200

    def tickets_by_tags(self, tags: list, limit: int = SEARCH_CAP) -> list:
        """Tickets carrying every tag given.

        Tags are not on the ticket object, and asking per ticket would be one
        request each across 3,800 tickets. Search resolves it server-side in
        about a second, which is also the only filter here that does not need
        the whole estate downloaded first.
        """
        clean = [str(t).strip() for t in tags if str(t).strip()]
        if not clean:
            return []
        query = " AND ".join(f'tags:"{t}"' for t in clean)
        return self.search(query, limit=limit)

    def ticket_tags(self, ticket_id) -> list:
        """Every tag on one ticket."""
        try:
            data = self.get("/api/v1/tags", {"object": "Ticket", "o_id": ticket_id})
            return list((data or {}).get("tags") or [])
        except Exception:
            return []

    def slas(self) -> list:
        """SLA definitions — the targets themselves, not per-ticket attainment.

        The ticket payload carries how far inside or past target each ticket
        landed, but not what the target was. Without these an answer can say
        "96% within SLA" and not what SLA.
        """
        try:
            return self.get("/api/v1/slas") or []
        except Exception:
            return []

    def time_accountings(self) -> list:
        """Booked time entries. Each carries ticket_id and time_unit."""
        try:
            return self.get("/api/v1/time_accountings") or []
        except Exception:
            return []

    def overviews(self) -> list:
        """The named queues agents actually work from."""
        try:
            return [o for o in (self.get("/api/v1/overviews") or []) if o.get("active")]
        except Exception:
            return []

    def overview_ticket_ids(self, link: str) -> list:
        """Ticket ids in one overview, by its `link` slug.

        Overview conditions are arbitrary Zammad expressions, so they are not
        re-implemented here — the server is asked to evaluate its own view.
        """
        try:
            data = self.get("/api/v1/ticket_overviews",
                            {"view": link, "per_page": 200}) or {}
            tickets = ((data.get("index") or {}).get("tickets")) or []
            return [t.get("id") for t in tickets if t.get("id") is not None]
        except Exception:
            return []


# ---------------------------------------------------------------------------
# Ceph
# ---------------------------------------------------------------------------
# The dashboard API is read-only from this app's point of view. It issues a
# short-lived JWT and offers no long-lived API key, so exactly one non-GET call
# exists in this client: the login. There is deliberately no create, update or
# delete method anywhere below, so nothing in the app can reach one.
_CEPH_TOKEN = {"value": None, "expires_at": 0.0}

# Endpoint versions are per-path and not guessable — /api/monitor answers at 1.0
# while /api/osd/settings wants 0.1, and asking with the wrong one returns 415,
# which reads like a malformed request rather than a header mismatch. Probed
# against Ceph 19.2.3 (squid).
CEPH_API_VERSIONS = {
    "/api/host": "1.3",
    "/api/block/image": "2.0",
    "/api/crush_rule": "2.0",
    "/api/osd/settings": "0.1",
}


class CephClient:
    """Read-only client for the Ceph mgr dashboard API."""

    def __init__(self):
        _ensure_config()
        self.url = os.getenv("CEPH_URL", "").rstrip("/")
        # The dashboard's browser URL carries a frontend fragment that the API
        # layer never sees; strip it so a pasted address still works.
        if "/#" in self.url:
            self.url = self.url.split("/#")[0].rstrip("/")
        self.user = os.getenv("CEPH_USER", "")
        self.password = os.getenv("CEPH_PASS", "")
        self.verify = str(os.getenv("CEPH_VERIFY_TLS", "false")).lower() in ("1", "true", "yes")
        if not self.verify:
            try:
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            except Exception:
                pass

    @property
    def configured(self) -> bool:
        return bool(self.url and self.user and self.password)

    def _token(self) -> Optional[str]:
        """A valid JWT, logging in only when the cached one has expired.

        The token lasts about eight hours. Renewing on expiry rather than per
        call keeps the login count to roughly three a day.
        """
        now = time.time()
        if _CEPH_TOKEN["value"] and now < _CEPH_TOKEN["expires_at"] - 60:
            return _CEPH_TOKEN["value"]

        # The one POST in the app, and it creates nothing — it exchanges
        # credentials for a token, so a second attempt is safe.
        response = request(
            "POST", f"{self.url}/api/auth", system="Ceph",
            json={"username": self.user, "password": self.password},
            headers={"Accept": "application/vnd.ceph.api.v1.0+json",
                     "Content-Type": "application/json"},
            verify=self.verify, timeout=20)
        if response.status_code not in (200, 201):
            raise RuntimeError(
                f"Ceph rejected the login ({response.status_code}). "
                f"{str(response.text)[:150]}")

        payload = response.json()
        token = payload.get("token")
        if not token:
            raise RuntimeError("Ceph returned no token")

        # Trust the token's own expiry rather than assuming a lifetime.
        expires = now + 3600
        try:
            import base64 as _b64, json as _json
            claim = token.split(".")[1]
            body = _json.loads(_b64.urlsafe_b64decode(claim + "=" * (-len(claim) % 4)))
            if body.get("exp"):
                expires = float(body["exp"])
        except Exception:
            pass

        _CEPH_TOKEN["value"] = token
        _CEPH_TOKEN["expires_at"] = expires
        return token

    def get(self, path: str, params: Optional[dict] = None, version: Optional[str] = None):
        """One GET. The only request method this client exposes."""
        version = version or CEPH_API_VERSIONS.get(path.split("?")[0], "1.0")
        response = request(
            "GET", f"{self.url}{path}", system="Ceph",
            params=params,
            headers={"Accept": f"application/vnd.ceph.api.v{version}+json",
                     "Authorization": f"Bearer {self._token()}"},
            verify=self.verify, timeout=45)
        if response.status_code == 415:
            raise RuntimeError(
                f"Ceph refused the API version for {path}. Versions are per-endpoint; "
                f"tried v{version}.")
        response.raise_for_status()
        return response.json()

    # ---- read helpers, each carrying the parameter its answer depends on ----

    def health(self) -> dict:
        return self.get("/api/health/full")

    def summary(self) -> dict:
        return self.get("/api/summary")

    def pools(self) -> list:
        """Pools WITH statistics.

        Without stats=true every pool reports zero bytes, which reads as an
        empty cluster rather than a missing parameter.
        """
        return self.get("/api/pool", params={"stats": "true"}) or []

    def osds(self) -> list:
        return self.get("/api/osd") or []

    def hosts(self) -> list:
        return self.get("/api/host") or []

    def images(self) -> list:
        """Every RBD image.

        limit=-1 is mandatory. The default page returned 5 images out of 37 and
        omitted the `volumes` pool entirely — the one holding the Cinder data —
        so a caller trusting it would report that Cinder volumes do not exist.
        """
        return self.get("/api/block/image", params={"offset": 0, "limit": -1}) or []

    def alerts(self) -> list:
        data = self.get("/api/prometheus")
        return data if isinstance(data, list) else []

    def rgw_daemons(self) -> list:
        """Empty when RGW is not deployed, which makes bucket/user calls 500."""
        try:
            return self.get("/api/rgw/daemon") or []
        except Exception:
            return []

    # ---- families the 19 official questions need -------------------------

    def crush_rules(self) -> list:
        return self.get("/api/crush_rule") or []

    def cephfs(self) -> list:
        return self.get("/api/cephfs") or []

    def cephfs_clients(self, fs_id) -> list:
        """Clients with the filesystem mounted.

        Wrapped in {'status': 0, 'data': [...]}, and an empty list is a real
        finding — nothing is mounting it — not a failed call.
        """
        try:
            payload = self.get(f"/api/cephfs/{fs_id}/clients") or {}
            data = payload.get("data")
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def logs(self) -> dict:
        """Cluster log and audit log. Each entry carries a priority."""
        try:
            return self.get("/api/logs/all") or {}
        except Exception:
            return {}

    # Only these settings may ever leave this client. /api/settings returns
    # GRAFANA_API_PASSWORD in plaintext, and answering "what is the Grafana URL"
    # by handing the endpoint's output to a model would put that password into
    # the prompt and into the chat transcript. A whitelist, never a filter on
    # the way out.
    SAFE_SETTINGS = (
        "GRAFANA_API_URL",
        "GRAFANA_FRONTEND_API_URL",
        "PROMETHEUS_API_HOST",
        "ALERTMANAGER_API_HOST",
        "GRAFANA_UPDATE_DASHBOARDS",
    )

    def endpoints(self) -> dict:
        """The monitoring URLs Ceph is configured against — whitelisted keys only."""
        try:
            rows = self.get("/api/settings") or []
        except Exception:
            return {}
        return {s.get("name"): s.get("value") for s in rows
                if s.get("name") in self.SAFE_SETTINGS}
