import re
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
from langchain_core.tools import tool

from tools.clients import OpenSearchClient, LOG_LEVEL_ALIASES, canonical_level

# ~4.9 million log lines arrive per day. Every query is time-bounded and answered
# with aggregations plus a small sample; fetching raw documents would blow the
# context window and tell the reader nothing.
DEFAULT_SAMPLE = 8
MAX_SAMPLE = 25

_RELATIVE = re.compile(r"(?:last|past)?\s*(\d+)\s*(minute|min|hour|hr|day|week|month)s?")
_ABSOLUTE = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})")


def _time_range(date_filter: Optional[str]) -> tuple:
    """Turn a phrase into an OpenSearch range plus a human label.

    Defaults to today rather than all time: the indices span 60 days and 135
    million documents, so an unbounded query is slow and almost never what was
    meant.
    """
    text = (date_filter or "today").strip().lower()

    absolute = _ABSOLUTE.search(text)
    if absolute:
        year, month, day = (int(g) for g in absolute.groups())
        stamp = f"{year:04d}-{month:02d}-{day:02d}"
        return {"gte": f"{stamp}T00:00:00", "lt": f"{stamp}T23:59:59.999"}, f"on {stamp}"

    if "yesterday" in text:
        return {"gte": "now-1d/d", "lt": "now/d"}, "yesterday"
    if "today" in text or "so far" in text:
        return {"gte": "now/d", "lte": "now"}, f"today ({datetime.now(timezone.utc).date()})"
    if "this week" in text:
        return {"gte": "now/w", "lte": "now"}, "this week"
    if "this month" in text:
        return {"gte": "now/M", "lte": "now"}, "this month"

    relative = _RELATIVE.search(text)
    if relative:
        amount, unit = int(relative.group(1)), relative.group(2)
        code = {"minute": "m", "min": "m", "hour": "h", "hr": "h",
                "day": "d", "week": "w", "month": "M"}[unit]
        return {"gte": f"now-{amount}{code}", "lte": "now"}, f"the last {amount} {unit}(s)"

    return {"gte": "now/d", "lte": "now"}, f"today ({datetime.now(timezone.utc).date()})"


def _level_terms(log_level: Optional[str]) -> list:
    """Every spelling of the requested severity, so counts are not silently wrong."""
    if not log_level:
        return []
    wanted = []
    for token in re.split(r"[,\s]+", str(log_level).lower()):
        token = token.strip()
        if not token:
            continue
        if token in ("err",):
            token = "error"
        if token in ("warn",):
            token = "warning"
        if token in LOG_LEVEL_ALIASES:
            wanted.extend(LOG_LEVEL_ALIASES[token])
    return wanted


def _build_query(search, time_range, levels, service, host_filter, tenant, exclude_debug):
    filters = [{"range": {"@timestamp": time_range}}]
    must = []

    if search:
        # Full text across the message and the fields that name its origin, so
        # "rabbit" or "no port found" both work.
        must.append({"simple_query_string": {
            "query": search,
            "fields": ["Payload", "programname", "python_module", "Logger", "http_url"],
            "default_operator": "and",
        }})
    if levels:
        filters.append({"terms": {"log_level.keyword": levels}})
    elif exclude_debug:
        # DEBUG is over half of all volume and is noise for an operator question.
        filters.append({"bool": {"must_not": [
            {"terms": {"log_level.keyword": LOG_LEVEL_ALIASES["debug"]}}]}})
    if service:
        must.append({"simple_query_string": {
            "query": service, "fields": ["programname", "python_module", "Logger"],
            "default_operator": "and"}})
    if host_filter:
        must.append({"simple_query_string": {
            "query": host_filter, "fields": ["Hostname"], "default_operator": "and"}})
    if tenant:
        must.append({"simple_query_string": {
            "query": tenant, "fields": ["tenant_id"], "default_operator": "and"}})

    query = {"bool": {"filter": filters}}
    if must:
        query["bool"]["must"] = must
    return query


@tool
def query_opensearch(
    search: Optional[str] = None,
    date_filter: Optional[str] = None,
    log_level: Optional[str] = None,
    service: Optional[str] = None,
    host_filter: Optional[str] = None,
    tenant: Optional[str] = None,
    group_by: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict:
    """
    Search the OpenStack application logs (OpenSearch, flog-* indices, shipped by
    fluentd from Kolla). Use for what OpenStack SERVICES are doing or failing —
    nova, neutron, keystone, glance, cinder, placement, heat, rabbit, haproxy —
    plus free-text log search and error investigation.

    SCOPE: OpenStack service logs from server01/02/03 ONLY. NOTHING about network
    devices — no Cisco routers or switches, no interface up/down, no SNMP. A
    question about a router, switch or "network device down" goes to query_zabbix,
    NOT here. Zero hits here means "not logged here", NOT "nothing is wrong".

    - search: text to find, e.g. 'no port found', 'timeout'. Supports quoted
      phrases, AND/OR and -negation.
    - date_filter: window, defaulting to today. Set only when the user names a
      period. Accepted: a day, a rolling window of N minutes/hours/days, a calendar
      period, or an exact date. Retention is about 60 days.
    - log_level: error, warning, info, debug, critical (comma separated). Case
      variants handled automatically. Omitted excludes DEBUG, over half of volume.
    - service: e.g. 'nova-api', 'neutron-server', 'keystone', 'haproxy'.
    - host_filter: server01/02/03. tenant: OpenStack project id.
    - group_by: ranked breakdown — service, level, host, module, hour.
    - limit: example log lines returned (default 8, max 25).
    """
    client = OpenSearchClient()
    time_range, window_label = _time_range(date_filter)
    levels = _level_terms(log_level)
    sample_size = max(1, min(int(limit or DEFAULT_SAMPLE), MAX_SAMPLE))

    query = _build_query(search, time_range, levels, service, host_filter, tenant,
                         exclude_debug=not levels)

    aggregation_field = {
        "service": "programname.keyword", "program": "programname.keyword",
        "level": "log_level.keyword", "severity": "log_level.keyword",
        "host": "Hostname.keyword", "server": "Hostname.keyword",
        "module": "python_module.keyword", "tenant": "tenant_id.keyword",
    }.get((group_by or "").strip().lower())

    body = {
        "size": sample_size,
        "track_total_hits": True,          # otherwise totals silently cap at 10,000
        "sort": [{"@timestamp": {"order": "desc"}}],
        "query": query,
        "aggs": {
            "by_level": {"terms": {"field": "log_level.keyword", "size": 20}},
            "by_service": {"terms": {"field": "programname.keyword", "size": 10}},
            "by_host": {"terms": {"field": "Hostname.keyword", "size": 10}},
        },
    }
    if (group_by or "").strip().lower() == "hour":
        body["aggs"]["over_time"] = {
            "date_histogram": {"field": "@timestamp", "fixed_interval": "1h"}}
    elif aggregation_field:
        body["aggs"]["breakdown"] = {"terms": {"field": aggregation_field, "size": 15}}

    try:
        response = client.search(body)
    except Exception as exc:
        return {"summary": f"OpenSearch query failed: {type(exc).__name__}: {str(exc)[:250]}",
                "dataframe": pd.DataFrame(), "raw": {"error": str(exc), "query": query}}

    total = response.get("hits", {}).get("total", {}).get("value", 0)
    hits = response.get("hits", {}).get("hits", [])
    aggs = response.get("aggregations", {})

    rows = []
    for hit in hits:
        source = hit.get("_source", {})
        # Access logs carry no level and no Timestamp field, only @timestamp.
        raw_level = source.get("log_level")
        stamp = str(source.get("Timestamp") or source.get("@timestamp") or "")[:23]
        message = " ".join(str(source.get("Payload") or source.get("http_url") or "").split())
        rows.append({
            "time": stamp,
            "level": canonical_level(raw_level).upper() if raw_level else "-",
            "service": source.get("programname", ""),
            "host": source.get("Hostname", ""),
            "message": message[:220],
            "module": source.get("python_module", ""),
        })
    df = pd.DataFrame(rows)

    # Fold the many spellings of each severity into one bucket before reporting.
    level_counts = {}
    for bucket in aggs.get("by_level", {}).get("buckets", []):
        name = canonical_level(bucket["key"]).upper()
        level_counts[name] = level_counts.get(name, 0) + bucket["doc_count"]
    level_counts = dict(sorted(level_counts.items(), key=lambda kv: kv[1], reverse=True))

    scope = []
    if search:
        scope.append(f"matching '{search}'")
    if log_level:
        scope.append(f"level {log_level}")
    if service:
        scope.append(f"service '{service}'")
    if host_filter:
        scope.append(f"host '{host_filter}'")
    if tenant:
        scope.append(f"tenant '{tenant}'")
    scope_text = (" " + ", ".join(scope)) if scope else ""

    if total == 0:
        message = f"No OpenStack service logs{scope_text} for {window_label}. "
        # Only steer towards Zabbix when the question actually looks like it was
        # about network kit — otherwise the redirect is noise on an ordinary
        # "no errors today", which is a perfectly good answer.
        haystack = " ".join(str(x) for x in (search, service, host_filter) if x).lower()
        if any(word in haystack for word in
               ("cisco", "fastethernet", "interface", "router", "switch", "snmp",
                "192.168.1.7", "192.168.1.8", "link", "port down")):
            message += ("NOTE: this index holds OpenStack application logs from "
                        "server01/02/03 only — network devices never log here. Use "
                        "query_zabbix for router, switch and interface events. Zero hits "
                        "here does NOT mean nothing is wrong.")
        else:
            message += ("The index was reachable and the window valid, so this is a real "
                        "zero rather than a failure. Widen date_filter, drop log_level, or "
                        "loosen the search terms to see more.")
        return {"summary": message, "dataframe": df, "raw": {"query": query, "total": 0}}

    summary = f"OpenStack service logs{scope_text} for {window_label}: {total:,} entries. "
    if not log_level:
        summary += "(DEBUG excluded — it is over half of all log volume.) "
    if level_counts:
        summary += "By level: " + ", ".join(f"{k} {v:,}" for k, v in level_counts.items())
        # Access logs (keystone-apache-public-access, nova-api-access, haproxy)
        # carry no log_level, so the level counts do not add up to the total.
        # Saying so stops the model inventing a category to reconcile them.
        unlevelled = total - sum(level_counts.values())
        if unlevelled > 0:
            summary += (f", plus {unlevelled:,} entries with no level field "
                        f"(HTTP access logs)")
        summary += ". "

    services = aggs.get("by_service", {}).get("buckets", [])
    if services:
        summary += ("Top services: "
                    + ", ".join(f"{b['key']} {b['doc_count']:,}" for b in services[:6]) + ". ")
    hosts = aggs.get("by_host", {}).get("buckets", [])
    if hosts:
        summary += "By host: " + ", ".join(f"{b['key']} {b['doc_count']:,}" for b in hosts) + ". "

    if "breakdown" in aggs:
        buckets = aggs["breakdown"]["buckets"]
        summary += (f"BREAKDOWN BY {group_by.upper()}: "
                    + ", ".join(f"{b['key']}: {b['doc_count']:,}" for b in buckets) + ". ")
        df = pd.DataFrame([{group_by: b["key"], "entries": b["doc_count"]} for b in buckets])
    elif "over_time" in aggs:
        buckets = [b for b in aggs["over_time"]["buckets"] if b["doc_count"]]
        summary += ("BY HOUR: "
                    + ", ".join(f"{b['key_as_string'][11:16]} {b['doc_count']:,}"
                                for b in buckets) + ". ")
        df = pd.DataFrame([{"hour": b["key_as_string"], "entries": b["doc_count"]}
                           for b in buckets])

    if rows:
        summary += f"Most recent {len(rows)} of {total:,}: " + " | ".join(
            f"[{r['time']}] {r['level']} {r['service']} on {r['host']}: {r['message'][:130]}"
            for r in rows)

    return {"summary": summary, "dataframe": df,
            "raw": {"query": query, "total": total, "levels": level_counts}}
