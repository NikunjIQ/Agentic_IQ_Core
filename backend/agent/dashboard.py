"""The dashboard's numbers: each system's headline figures, straight from its API.

No model is involved anywhere in this file, so loading or refreshing the
dashboard costs no tokens. Every figure is computed here from the same clients
the tools use, never parsed back out of a tool's summary text.

Each system is its own request and fails on its own: one that is not connected
for this user, or is down, becomes a tile that says so, and the rest still
load — each as soon as its own system answers, not when the slowest does. The systems watch different estates (HANDOVER.md §1), so nothing
here adds a number from one to a number from another.

Kept off deliberately, because each takes long enough to stall the page: the
full Zammad ticket download (15-43s), Ceph's image listing (10-45s, flaky) and
Zabbix change history (~137s).
"""

import time
import contextvars
import concurrent.futures
from collections import Counter
from datetime import datetime, timedelta, timezone

from agent import config_store
from tools.clients import (Cached, CephClient, OpenSearchClient, PrometheusClient,
                           ZabbixClient, ZammadClient, LOG_LEVEL_ALIASES,
                           SEVERITY_LABELS, canonical_level)

CACHE_SECONDS = 60
# The OSD layout changes when hardware does, not minute to minute.
_OSD_CACHE = Cached("dashboard ceph osds", 300)

GB = 1024 ** 3
TB = 1024 ** 4


def _together(*calls) -> list:
    """Run independent requests side by side, each as this thread's signed-in user.

    A context variable does not follow into a new thread, and a client built
    there without it would read the wrong credentials, or none.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [pool.submit(contextvars.copy_context().run, call) for call in calls]
        return [future.result() for future in futures]


def _number(series: dict):
    try:
        return round(float(series["value"][1]), 1)
    except (KeyError, IndexError, TypeError, ValueError):
        return None


# ------------------------------------------------------------------- zabbix
def zabbix() -> dict:
    from tools.zabbix.common import derive_availability
    client = ZabbixClient()

    hosts = [h for h in client.hosts() if str(h.get("status", "0")) == "0"]   # monitored only
    states = Counter(derive_availability(h) for h in hosts)
    down = [h.get("name") or h.get("host") for h in hosts
            if derive_availability(h) in ("down", "unreachable")]

    limit = 500
    problems = client.problems(limit=limit)
    by_code = Counter(int(p.get("severity", 0) or 0) for p in problems)
    return {
        "hosts": {"total": len(hosts), "up": states["up"],
                  "down": states["down"] + states["unreachable"],
                  "unknown": states["unknown"], "down_names": sorted(down)[:8]},
        "problems": {
            "total": len(problems), "capped": len(problems) >= limit,
            "unacknowledged": sum(1 for p in problems if str(p.get("acknowledged")) != "1"),
            "by_severity": [{"severity": SEVERITY_LABELS[code], "count": by_code[code]}
                            for code in sorted(SEVERITY_LABELS, reverse=True)],
        },
    }


# ------------------------------------------------------------------- zammad
def zammad() -> dict:
    from tools.zammad_tool import IST, _range_query
    client = ZammadClient()
    days = 14
    now = datetime.now(IST)
    start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)

    # Search, never the full estate download. search_all returns None past
    # 1,000 matches, and that is reported as "too many" rather than guessed at.
    backlog, created, closed = _together(
        lambda: client.search_all('(state.name:"new" OR state.name:"open")'),
        lambda: client.search_all(_range_query("created_at", (start, None))),
        lambda: client.search_all(_range_query("close_at", (start, None))))

    def per_day(tickets, field):
        if tickets is None:
            return None
        counts = Counter()
        for ticket in tickets:
            stamp = ticket.get(field)
            if stamp:
                when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).astimezone(IST)
                counts[when.date().isoformat()] += 1
        return counts

    created_by_day, closed_by_day = per_day(created, "created_at"), per_day(closed, "close_at")
    trend = None
    if created_by_day is not None and closed_by_day is not None:
        trend = [{"day": day, "created": created_by_day[day], "closed": closed_by_day[day]}
                 for day in ((start + timedelta(days=n)).date().isoformat() for n in range(days))]

    today = now.date().isoformat()
    return {
        "backlog": None if backlog is None else {
            "open": len(backlog),
            # Zammad's "nobody" owner is user 1.
            "unassigned": sum(1 for t in backlog if t.get("owner_id") in (1, None)),
        },
        "created_today": None if created_by_day is None else created_by_day[today],
        "closed_today": None if closed_by_day is None else closed_by_day[today],
        "trend": trend, "days": days,
    }


# --------------------------------------------------------------- prometheus
def prometheus() -> dict:
    from tools.prometheus_tool import _INTENTS
    client = PrometheusClient()
    names = {instance: name for name, instance in client.node_map().items()}

    def query(intent: str, window: str = "5m") -> str:
        return _INTENTS[intent][1].format(sel="", selb="", w=window)

    servers = {}
    # A host has several filesystems; the fullest one is the one that matters.
    for key, promql in (("cpu", query("cpu")), ("memory", query("memory")),
                        ("disk", f"max by (instance) ({query('disk')})")):
        for series in client.instant(promql):
            instance = series.get("metric", {}).get("instance")
            if instance in names:
                servers.setdefault(names[instance], {})[key] = _number(series)

    history = []
    for series in client.over_time(query("cpu", "30m"), minutes=1440, step="1800s"):
        instance = series.get("metric", {}).get("instance")
        if instance in names:
            history.append({"host": names[instance],
                            "points": [[int(t) * 1000, round(float(v), 1)]
                                       for t, v in series.get("values", [])]})

    targets = client.targets()
    vms = client.instant("libvirt_domain_info_state")
    return {
        "servers": [{"host": host, **values} for host, values in sorted(servers.items())],
        "cpu_history": sorted(history, key=lambda s: s["host"]),
        "targets": {"total": len(targets),
                    "up": sum(1 for t in targets if t.get("health") == "up")},
        "vms": {"total": len(vms), "running": sum(1 for s in vms if _number(s) == 1)},
    }


# --------------------------------------------------------------- opensearch
def opensearch() -> dict:
    client = OpenSearchClient()
    bad = LOG_LEVEL_ALIASES["error"] + LOG_LEVEL_ALIASES["critical"]
    errors_only = {"terms": {"log_level.keyword": bad}}

    today = client.search({
        "size": 0, "track_total_hits": True,       # otherwise totals cap at 10,000
        "query": {"range": {"@timestamp": {"gte": "now/d", "lte": "now"}}},
        "aggs": {
            "by_level": {"terms": {"field": "log_level.keyword", "size": 20}},
            "errors": {"filter": errors_only, "aggs": {
                "by_service": {"terms": {"field": "programname.keyword", "size": 6}}}},
        }})
    levels = Counter()
    for bucket in today["aggregations"]["by_level"]["buckets"]:
        levels[canonical_level(bucket["key"])] += bucket["doc_count"]

    hourly = client.search({
        "size": 0,
        "query": {"bool": {"filter": [
            {"range": {"@timestamp": {"gte": "now-24h", "lte": "now"}}}, errors_only]}},
        "aggs": {"over_time": {"date_histogram": {
            "field": "@timestamp", "fixed_interval": "1h", "min_doc_count": 0,
            "extended_bounds": {"min": "now-24h", "max": "now"}}}}})
    return {
        "lines_today": today["hits"]["total"]["value"],
        "errors_today": levels["error"] + levels["critical"],
        "warnings_today": levels["warning"],
        "errors_by_service": [{"service": b["key"], "errors": b["doc_count"]}
                              for b in today["aggregations"]["errors"]["by_service"]["buckets"]],
        "errors_by_hour": [[b["key"], b["doc_count"]]
                           for b in hourly["aggregations"]["over_time"]["buckets"]],
    }


# --------------------------------------------------------------------- ceph
def ceph() -> dict:
    client = CephClient()
    health = client.health()
    stats = (health.get("df") or {}).get("stats") or {}
    pg_states = (health.get("pg_info") or {}).get("statuses") or {}
    osds = (health.get("osd_map") or {}).get("osds") or []
    checks = (health.get("health") or {}).get("checks") or []
    pgs = sum(pg_states.values())

    layout = _OSD_CACHE.get()
    if layout is None:
        layout = _OSD_CACHE.put([{
            "host": (o.get("host") or {}).get("name", "?"),
            "osd": f"osd.{o.get('osd')}",
            "size_gb": round((o.get("stats") or {}).get("stat_bytes", 0) / GB, 1),
            "up": "up" in (o.get("state") or []),
        } for o in client.osds()])

    return {
        "status": (health.get("health") or {}).get("status", "UNKNOWN"),
        "checks": [{"type": c.get("type"), "message": (c.get("summary") or {}).get("message")}
                   for c in checks[:5]],
        # Raw, not usable: every pool is replicated, so less than this can be stored.
        "raw": {"total_tb": round((stats.get("total_bytes") or 0) / TB, 2),
                "used_tb": round((stats.get("total_used_raw_bytes") or 0) / TB, 2),
                "used_pct": round((stats.get("total_used_raw_ratio") or 0) * 100, 1)},
        "osds": {"total": len(osds), "up": sum(1 for o in osds if "up" in (o.get("state") or []))},
        "pgs": {"total": pgs, "clean": pg_states.get("active+clean", 0),
                "clean_pct": round(pg_states.get("active+clean", 0) / pgs * 100, 1) if pgs else None},
        "osd_layout": layout,
    }


# ------------------------------------------------------------- ceph daemons
def ceph_daemons() -> dict:
    # Through the tool rather than the client: which lines count as a problem,
    # and how near-identical ones are grouped, is decided there once.
    from tools.ceph_daemon_tool import check_ceph_daemon_health
    problems, daemons = _together(
        lambda: check_ceph_daemon_health.invoke({"query_type": "problems", "limit": 5}),
        lambda: check_ceph_daemon_health.invoke({"query_type": "daemons"}))
    if "not configured" in problems["summary"]:
        raise config_store.NotConnected("Ceph container logs isn't connected for your "
                                        "account — add it on the Connections page.")
    return {
        "distinct_problems": problems["raw"].get("distinct", 0),
        "lines_checked": problems["raw"].get("checked", 0),
        "top_problems": problems["dataframe"].to_dict(orient="records"),
        "daemons": daemons["raw"].get("daemons", []),
    }


# ---------------------------------------------------- zabbix x zammad join
def unticketed() -> dict:
    from tools.correlation import correlate
    gaps = correlate(only_gaps=True)["df"]
    rows = [] if gaps.empty else gaps.head(6)[
        ["host", "problem", "severity", "age_hours"]].to_dict(orient="records")
    return {"count": int(len(gaps)), "top": rows}


SOURCES = {"zabbix": zabbix, "zammad": zammad, "prometheus": prometheus,
           "opensearch": opensearch, "ceph": ceph, "ceph_daemons": ceph_daemons,
           "unticketed": unticketed}
_CACHES = {name: Cached(f"dashboard {name}", CACHE_SECONDS) for name in SOURCES}


def fetch(user_id, name: str, fresh: bool = False) -> dict:
    """One system's figures for one user, cached for a minute.

    Never raises: a system that is not connected, or is down, is a result the
    page shows on that tile, not a failed request.
    """
    config_store.use_user(user_id)      # whose credentials, and whose cache slot
    try:
        cached = None if fresh else _CACHES[name].get()
        if cached is not None:
            return cached
        started = time.time()
        try:
            result = {"status": "ok", "data": SOURCES[name]()}
        except config_store.NotConnected as exc:
            result = {"status": "not_connected", "message": str(exc)}
        except Exception as exc:
            result = {"status": "error", "message": f"{type(exc).__name__}: {str(exc)[:200]}"}
        result["seconds"] = round(time.time() - started, 1)
        result["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return _CACHES[name].put(result)
    finally:
        config_store.use_user(None)
