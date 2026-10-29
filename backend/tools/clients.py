"""
Thin API clients for Zabbix and Zammad.

The @tool functions each grew their own copy of auth + request plumbing. The
correlation engine needs the same plumbing without going through the tool
wrappers, so it lives here once.
"""

import hashlib
import os
import time
import concurrent.futures
from typing import Optional

import requests
from requests.auth import HTTPBasicAuth


def _ensure_config(service: str = None):
    """Apply saved system settings, then check the caller's account.

    With a signed-in user in context, a service they have not connected raises
    config_store.NotConnected here — in the client's constructor, before any
    request is made — rather than falling back to someone else's credentials.
    """
    try:
        from agent.config_store import ensure_applied
        ensure_applied()
    except Exception:
        pass
    if service:
        from agent.config_store import require
        require(service)


def cfg(key: str, default: str = "") -> str:
    """A setting for the current request: the signed-in user's own value for a
    service field, the environment for everything else.

    Read in constructors only. ZammadClient.tickets_by_id fans work out over a
    thread pool, and a pooled thread does not carry the request's user — it is
    safe because the credentials were captured in __init__, before the pool.
    """
    from agent.config_store import get
    return get(key, default)


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


def request(method: str, url: str, *, system: str = "", retry: bool = True, **kwargs):
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

    retry=False is for writes. A POST that times out may already have been
    stored, and sending it again would create the thing twice.
    """
    attempts = (_retry_budget() if retry else 0) + 1
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


class Cached:
    """One value per signed-in user, with a TTL, plus a registry so every cache
    can be dropped at once.

    Per user, not per process: these hold data fetched with somebody's own
    credentials — their Zammad tickets, their Zabbix devices — and one shared
    slot would serve one account's estate to another. With nobody signed in (a
    script, the eval harness) everything shares the None slot, as before.

    The registry exists because config_store.reset_caches used to reach into four
    modules by name inside a bare `except`, where a renamed key fails silently.
    """

    _registry = []

    def __init__(self, name: str, ttl):
        # `ttl` may be a number or a callable, so a value read from the
        # environment once at import and one that can change are both sayable.
        self.name = name
        self._ttl = ttl
        self._slots = {}              # owner -> (value, stored_at)
        Cached._registry.append(self)

    @property
    def ttl(self) -> float:
        return self._ttl() if callable(self._ttl) else self._ttl

    @property
    def value(self):
        """This owner's last value, expired or not — for "stale beats empty"."""
        slot = self._slots.get(_owner())
        return slot[0] if slot else None

    def get(self):
        """The cached value, or None when it is absent or has expired."""
        slot = self._slots.get(_owner())
        if slot and slot[0] is not None and time.time() - slot[1] < self.ttl:
            return slot[0]
        return None

    def put(self, value):
        self._slots[_owner()] = (value, time.time())
        return value

    def clear(self):
        self._slots.clear()

    @classmethod
    def clear_all(cls) -> list:
        """Drop every registered cache, for everyone; returns their names."""
        for cache in cls._registry:
            cache.clear()
        return [cache.name for cache in cls._registry]

    @classmethod
    def clear_owner_all(cls, owner) -> list:
        """Drop one user's entries from every cache, after they change settings."""
        for cache in cls._registry:
            cache._slots.pop(owner, None)
        return [cache.name for cache in cls._registry]


def _owner():
    try:
        from agent.config_store import current_user_id
        return current_user_id()
    except Exception:
        return None


# Pulling the whole ticket estate takes 12-30s. A question that needs two cuts of
# the same data should not pay for it twice.
#
# 60s is deliberately short for the running app: this is monitoring data and an
# answer should not be built on a stale estate. The eval suite raises it via
# ZAMMAD_CACHE_TTL, because a dozen Zammad checks in one run would otherwise
# re-download 3,788 tickets four or five times. That is a test concern only —
# changing the default would trade the app's freshness for speed, which is a
# separate decision.
TICKET_CACHE_TTL = int(os.getenv("ZAMMAD_CACHE_TTL", "60"))
_TICKET_CACHE = Cached("zammad tickets", TICKET_CACHE_TTL)


# ---------------------------------------------------------------------------
# Zabbix
# ---------------------------------------------------------------------------
class ZabbixClient:
    def __init__(self):
        # The same transport the Zabbix tools use, so there is one place that
        # signs in. Imported here because that module imports this one.
        from tools.zabbix.client import build_rpc
        self._rpc = build_rpc()

    def call(self, method: str, params: dict) -> list:
        return self._rpc(method, params)

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
PROM_NODE_CACHE_TTL = 300
_PROM_NODE_CACHE = Cached("prometheus nodes", PROM_NODE_CACHE_TTL)


class PrometheusClient:
    def __init__(self):
        _ensure_config("Prometheus")
        self.url = cfg("PROMETHEUS_URL", "http://192.168.1.29:9190").rstrip("/")
        user = cfg("PROMETHEUS_USER", "")
        password = cfg("PROMETHEUS_PASS", "")
        self.auth = HTTPBasicAuth(user, password) if user else None
        self.headers = {}
        token = cfg("PROMETHEUS_TOKEN", "")
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

    def node_map(self) -> dict:
        """Friendly hostname -> instance address, from node_uname_info.

        People ask about "server02", not "192.168.1.30:9100". Without this the
        host filter can only match raw addresses.
        """
        cached = _PROM_NODE_CACHE.get()
        if cached is not None:
            return cached

        mapping = {}
        try:
            for series in self.instant("node_uname_info"):
                labels = series.get("metric", {})
                name, instance = labels.get("nodename"), labels.get("instance")
                if name and instance:
                    mapping[name] = instance
        except Exception:
            pass

        return _PROM_NODE_CACHE.put(mapping)


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
        _ensure_config("OpenSearch")
        self.url = cfg("OPENSEARCH_URL", "http://192.168.1.32:9200").rstrip("/")
        self.index = cfg("OPENSEARCH_INDEX", "flog-*")
        user = cfg("OPENSEARCH_USER", "")
        password = cfg("OPENSEARCH_PASS", "")
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
        _ensure_config("Ceph container logs")
        raw = cfg("OPENSEARCH2_URL", "https://192.168.1.152:9200").rstrip("/")
        if not raw.startswith("http"):
            raw = "https://" + raw
        self.url = raw
        self.index = cfg("OPENSEARCH2_CEPH_INDEX", "podman-20*")
        user = cfg("OPENSEARCH2_USER", "")
        password = cfg("OPENSEARCH2_PASS", "")
        self.auth = HTTPBasicAuth(user, password) if user else None
        self.verify = str(cfg("OPENSEARCH2_VERIFY_TLS", "false")).lower() in (
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
    def __init__(self, service_token: str = None):
        # service_token: act as one fixed Zammad identity instead of the signed-in
        # user. Ticket assist runs from a webhook with nobody signed in, so it has
        # no account whose credentials it could borrow — and should not borrow any.
        if service_token:
            _ensure_config()
            self.url = os.getenv("ZAMMAD_URL", "http://192.168.1.205:8082").rstrip("/")
            self.headers = {"Authorization": f"Token token={service_token}"}
            self.auth = None
            return
        _ensure_config("Zammad")
        self.url = cfg("ZAMMAD_URL", "http://192.168.1.205:8082").rstrip("/")
        self.headers = {}
        self.auth = None

        token = cfg("ZAMMAD_API_TOKEN", "")
        if token:
            self.headers["Authorization"] = f"Token token={token}"
        else:
            user, password = cfg("ZAMMAD_USER", ""), cfg("ZAMMAD_PASS", "")
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

    # ---- writes ------------------------------------------------------------
    # The only writes this app makes to any system. They use their own token so
    # a user's own Zammad login (often an admin) is never what creates data, and
    # so writing can be revoked on the Zammad side without breaking anyone's reads.
    def _write(self, path: str, payload: dict) -> dict:
        token = os.getenv("ZAMMAD_WRITE_TOKEN", "")
        if not token:
            raise RuntimeError("ZAMMAD_WRITE_TOKEN is not set; refusing to write to Zammad")
        res = request("POST", f"{self.url}{path}", system="Zammad", retry=False,
                      headers={"Authorization": f"Token token={token}"},
                      json=payload, timeout=25)
        res.raise_for_status()
        return res.json() if res.content else {}

    def add_note(self, ticket_id, body: str) -> dict:
        """An internal note: visible to agents, never to the ticket's customer."""
        return self._write("/api/v1/ticket_articles", {
            "ticket_id": int(ticket_id), "body": body, "content_type": "text/plain",
            "type": "note", "internal": True})

    def add_tag(self, ticket_id, tag: str) -> dict:
        return self._write("/api/v1/tags/add", {
            "item": tag, "object": "Ticket", "o_id": int(ticket_id)})

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
        cached = _TICKET_CACHE.get()
        if cached is not None:
            return cached

        # Zammad caps per_page at 100 whatever is asked, so the estate is ~48
        # pages. Fetched 8 at a time: 4,794 tickets in ~14s rather than ~30s.
        tickets, page, workers = {}, 1, 8
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            while page <= max_pages:
                batches = list(pool.map(
                    lambda p: self.get("/api/v1/tickets", {"page": p, "per_page": page_size}) or [],
                    range(page, page + workers)))
                for batch in batches:
                    # By id: a ticket created mid-download shifts pages, and
                    # would otherwise appear twice.
                    tickets.update((t["id"], t) for t in batch)
                if any(len(batch) < page_size for batch in batches):
                    break
                page += workers
        tickets = list(tickets.values())

        if not tickets:  # search fallback, mirrors the original behaviour
            tickets = self.search("*", limit=1000)

        _TICKET_CACHE.put(tickets)
        return tickets

    def search(self, query: str, limit: int = 50, strict: bool = False,
               page: int = None) -> list:
        """Full-text ticket search — used for human-raised tickets that carry
        no Zabbix event id.

        strict raises instead of returning [], for a caller that would otherwise
        read a failed search as "no tickets matched". page reads past the first
        `limit` rows, which is the only way past SEARCH_CAP."""
        params = {"query": query, "limit": limit}
        if page:
            params.update(page=page, per_page=limit)
        try:
            data = self.get("/api/v1/tickets/search", params)
        except Exception:
            if strict:
                raise
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

    def search_all(self, query: str, max_rows: int = 1000) -> Optional[list]:
        """Every ticket matching a search, paged past SEARCH_CAP.

        None when more than max_rows match: past that, paging search is slower
        than the parallel estate download, which the caller should use instead.
        Raises on failure, so an error never reads as "nothing matched".
        """
        # ponytail: max_rows is a timing crossover measured on this estate
        # (search ~1.5s/200 rows vs ~14s for all 4,794); re-measure if either moves.
        first = self.search(query, limit=self.SEARCH_CAP, strict=True, page=1)
        if len(first) < self.SEARCH_CAP:
            return first
        data = self.get("/api/v1/tickets/search",
                        {"query": query, "limit": 1, "with_total_count": "true"})
        total = int((data or {}).get("total_count") or 0)
        if total > max_rows:
            return None
        pages = range(2, -(-total // self.SEARCH_CAP) + 1)
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            rest = pool.map(lambda p: self.search(query, limit=self.SEARCH_CAP,
                                                  strict=True, page=p), pages)
            found = {t["id"]: t for batch in [first, *rest] for t in batch}
        return list(found.values())

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
# One token per credential set, never one for the process: each user logs in
# with their own Ceph account, and a shared slot handed the first user's token
# to everyone for its ~8h lifetime, whatever their own URL or password.
_CEPH_TOKENS = {}

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
        _ensure_config("Ceph")
        self.url = cfg("CEPH_URL", "").rstrip("/")
        # The dashboard's browser URL carries a frontend fragment that the API
        # layer never sees; strip it so a pasted address still works.
        if "/#" in self.url:
            self.url = self.url.split("/#")[0].rstrip("/")
        self.user = cfg("CEPH_USER", "")
        self.password = cfg("CEPH_PASS", "")
        self.verify = str(cfg("CEPH_VERIFY_TLS", "false")).lower() in ("1", "true", "yes")
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
        key = (self.url, self.user, hashlib.sha256(self.password.encode()).hexdigest())
        token, expires_at = _CEPH_TOKENS.get(key, (None, 0.0))
        if token and now < expires_at - 60:
            return token

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

        _CEPH_TOKENS[key] = (token, expires)
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
