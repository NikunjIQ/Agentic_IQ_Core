"""Health of Ceph's containerised daemons, from their own stdout.

This is NOT the Ceph cluster log, and the distinction is the whole point of
the tool. `query_ceph(query_type='logs')` reads what the CLUSTER reported —
OSDs, placement groups, health checks, slow ops. This reads what Ceph's
CONTAINERS printed on server01. Probing the index found zero occurrences of
HEALTH_OK, HEALTH_WARN, slow ops, pg, mon, mgr, scrub or cluster, against 60
real cluster-log entries available from query_ceph at the same moment.

So it answers one thing nothing else can: a Ceph daemon can be broken while
the cluster reports HEALTH_OK. The live example this was built against —
`ERROR:ceph-crash:directory /var/lib/ceph/crash/posted does not exist` —
means crash reports are being dropped, and it is invisible to every other
tool in the app.

Two measured facts shape the code:

  heartbeat   83% of Ceph rows are the literal string "system:0", emitted by
              ceph-exporter every 5 seconds. Excluded by default, because
              otherwise it drowns everything that carries meaning.
  PRIORITY    NOT usable as severity. 60,513 of 61,219 documents are priority
              3, because anything written to stderr gets 3 regardless. A
              severity filter built on it returns 99% of the index, which is
              the same shape as the "down was a dead category" bug. Severity
              is read from the message text instead.
"""

import re
from typing import Optional

import pandas as pd
from langchain_core.tools import tool

from tools.clients import CephDaemonLogClient

# Every field in this index is `text` with a `.keyword` subfield. Aggregating
# the bare field returns EMPTY BUCKETS rather than an error, so a breakdown
# silently comes back blank. Always aggregate on .keyword.
CONTAINER = "CONTAINER_NAME.keyword"
UNIT = "SYSTEMD_UNIT.keyword"

DEFAULT_SAMPLE = 8
MAX_SAMPLE = 25

# Severity has to come from the text, since PRIORITY cannot carry it.
_CONCERNING = re.compile(r"\b(error|err|fatal|fail(ed|ure)?|warn(ing)?|critical|"
                         r"panic|exception|cannot|denied|refused)\b", re.I)

# Ceph names its containers ceph-<fsid>-<daemon>-<host>; the fsid is noise in
# an answer and makes every name unreadably long.
_FSID = re.compile(r"ceph-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-")


def _short(name: str) -> str:
    return _FSID.sub("", str(name or "")) or "(unnamed)"


def _time_filter(date_filter: Optional[str]) -> tuple:
    """A range clause plus a label. Defaults to the whole index.

    Unlike the OpenStack log tool, which defaults to today because its indices
    hold 135M documents over 60 days, this index is small and shallow — the
    useful default is everything it has.
    """
    text = (date_filter or "").strip().lower()
    if not text:
        return None, "the whole index"
    match = re.search(r"(\d+)\s*(minute|min|hour|hr|day)s?", text)
    if match:
        amount, unit = match.group(1), match.group(2)[0]
        unit = {"m": "m", "h": "h", "d": "d"}.get(unit, "h")
        return {"gte": f"now-{amount}{unit}"}, f"the last {amount} {match.group(2)}(s)"
    if "today" in text:
        return {"gte": "now/d"}, "today"
    if "yesterday" in text:
        return {"gte": "now-1d/d", "lt": "now/d"}, "yesterday"
    return None, "the whole index"


def _coverage_note(client, window_label: str) -> str:
    """State what the index actually holds, always.

    The index is daily-rotated and was six hours deep when this was written.
    A question about last week finds nothing, and an unqualified "no results"
    reads as "nothing happened" rather than "not collected yet" — the exact
    distinction coverage.py exists to protect elsewhere in this app.
    """
    span = client.coverage()
    if not span or not span.get("first"):
        return ""
    return (f" Coverage: this index holds {span.get('documents', 0):,} document(s) "
            f"from {span['first'][:16].replace('T', ' ')} to "
            f"{span['last'][:16].replace('T', ' ')} UTC only — it is rotated daily "
            f"and started recently, so an empty result for an earlier period means "
            f"NOT COLLECTED YET, not that nothing happened.")


@tool
def check_ceph_daemon_health(
    query_type: Optional[str] = "problems",
    daemon: Optional[str] = None,
    search: Optional[str] = None,
    date_filter: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict:
    """
    Health of Ceph's own daemon CONTAINERS on server01, from their stdout.
    A daemon can be broken while the cluster reports HEALTH_OK.

    NOT for disks, OSDs, pools, capacity, slow ops or cluster health — none of
    those exist in this data. Those are query_ceph.

    - query_type: 'problems' (default) container errors | 'daemons' which are
      logging and when each was last heard from | 'search' free text.
    - daemon: one only, e.g. 'crash', 'ceph-exporter', 'grafana'.
    - search: text to find (required for 'search').
    - date_filter: e.g. 'last 2 hours', 'today'. Defaults to all held.
    - limit: example lines (default 8, max 25).
    """
    client = CephDaemonLogClient()
    if not client.configured:
        return {"summary": "The Ceph daemon log cluster is not configured "
                           "(OPENSEARCH2_URL / USER / PASS).",
                "dataframe": pd.DataFrame(), "raw": {}}

    kind = (query_type or "problems").strip().lower()
    sample = max(1, min(int(limit or DEFAULT_SAMPLE), MAX_SAMPLE))
    time_range, window = _time_filter(date_filter)

    filters = [{"wildcard": {CONTAINER: "*ceph*"}}]
    if time_range:
        filters.append({"range": {"@timestamp": time_range}})
    if daemon:
        filters.append({"wildcard": {CONTAINER: f"*{daemon.strip().lower()}*"}})
    if search:
        filters.append({"match_phrase": {"MESSAGE": search}})

    try:
        # ---- which daemons are alive, and when each was last heard from ----
        if kind in ("daemons", "liveness", "alive", "units"):
            data = client.search({"size": 0, "query": {"bool": {"filter": filters}},
                                  "aggs": {"d": {"terms": {"field": CONTAINER, "size": 25},
                                                 "aggs": {"last": {"max": {"field": "@timestamp"}},
                                                          "first": {"min": {"field": "@timestamp"}}}}}})
            rows = [{
                "daemon": _short(b["key"]),
                "log_lines": b["doc_count"],
                "first_seen": (b["first"].get("value_as_string") or "")[:19].replace("T", " "),
                "last_seen": (b["last"].get("value_as_string") or "")[:19].replace("T", " "),
            } for b in data["aggregations"]["d"]["buckets"]]
            table = pd.DataFrame(rows)
            if not rows:
                return {"summary": "No Ceph daemon containers are logging to this index."
                                   + _coverage_note(client, window),
                        "dataframe": table, "raw": {}}
            summary = (f"{len(rows)} Ceph daemon container(s) logging on server01 over "
                       f"{window}: "
                       + "; ".join(f"{r['daemon']} ({r['log_lines']:,} lines, last at "
                                   f"{r['last_seen']})" for r in rows) + ". "
                       + "A daemon that stopped logging would show an older last_seen — "
                         "ceph-exporter writes every 5 seconds, so a gap there is real. "
                         "This is container liveness, NOT cluster health: use query_ceph "
                         "for OSDs, pools and disk failures.")
            return {"summary": summary + _coverage_note(client, window),
                    "dataframe": table, "raw": {"daemons": rows}}

        # ---- free-text search -------------------------------------------
        if kind in ("search", "text", "find", "grep"):
            if not search:
                return {"summary": "query_type='search' needs a search term.",
                        "dataframe": pd.DataFrame(), "raw": {}}
            data = client.search({"size": sample, "query": {"bool": {"filter": filters}},
                                  "sort": [{"@timestamp": {"order": "desc"}}]})
            total = data["hits"]["total"]["value"]
            rows = [{"at": h["_source"]["@timestamp"][:19].replace("T", " "),
                     "daemon": _short(h["_source"].get("CONTAINER_NAME")),
                     "message": str(h["_source"].get("MESSAGE") or "").strip()[:300]}
                    for h in data["hits"]["hits"]]
            summary = (f"{total:,} line(s) from Ceph daemon containers match "
                       f"'{search}' over {window}"
                       + (f", showing the {len(rows)} most recent." if rows else "."))
            return {"summary": summary + _coverage_note(client, window),
                    "dataframe": pd.DataFrame(rows), "raw": {"total": total}}

        # ---- problems: the default, and the reason this tool exists -------
        # The heartbeat is excluded here rather than in the filter list so the
        # 'search' branch above can still find it if somebody asks directly.
        problem_filters = filters + [
            {"bool": {"must_not": [{"match_phrase": {"MESSAGE": client.HEARTBEAT}}]}}]

        # Counts come from an aggregation, not from counting sampled documents.
        # Sampling the newest 200 reported the crash error as "seen 167x since
        # 10:25" when the true figure is 725 since 05:47 — a wrong number
        # stated confidently, which is exactly what this codebase computes in
        # code rather than leaving to a sample.
        data = client.search({
            "size": 0, "query": {"bool": {"filter": problem_filters}},
            "aggs": {"messages": {
                "terms": {"field": "MESSAGE.keyword", "size": 200},
                "aggs": {"first": {"min": {"field": "@timestamp"}},
                         "last": {"max": {"field": "@timestamp"}},
                         "daemon": {"terms": {"field": CONTAINER, "size": 1}}}}}})
        total_non_heartbeat = data["hits"]["total"]["value"]
        buckets = data["aggregations"]["messages"]["buckets"]

        # MESSAGE.keyword has ignore_above=256, so anything longer is absent
        # from the aggregation entirely. Measure the shortfall rather than let
        # it pass silently — 104 of 858 lines fell outside it when written.
        grouped_total = sum(b["doc_count"] for b in buckets)
        ungrouped = max(0, total_non_heartbeat - grouped_total)

        seen = {}
        for bucket in buckets:
            message = str(bucket["key"]).strip()
            if not _CONCERNING.search(message):
                continue
            daemon_buckets = bucket["daemon"]["buckets"]
            daemon = _short(daemon_buckets[0]["key"]) if daemon_buckets else "(unknown)"
            # Group near-identical lines: Grafana embeds a timestamp in every
            # message, so each is its own bucket and they would never collapse.
            key = (re.sub(r"\d+", "#", message)[:120], daemon)
            entry = seen.setdefault(key, {
                "daemon": daemon, "message": message[:300],
                "occurrences": 0, "first_seen": None, "last_seen": None})
            entry["occurrences"] += bucket["doc_count"]
            first = (bucket["first"].get("value_as_string") or "")[:19].replace("T", " ")
            last = (bucket["last"].get("value_as_string") or "")[:19].replace("T", " ")
            entry["first_seen"] = min(entry["first_seen"] or first, first)
            entry["last_seen"] = max(entry["last_seen"] or last, last)

        rows = sorted(seen.values(), key=lambda r: -r["occurrences"])[:sample]
        table = pd.DataFrame(rows)

        if not rows:
            return {"summary": (f"No errors or warnings from Ceph's daemon containers over "
                                f"{window}. {total_non_heartbeat:,} non-heartbeat line(s) "
                                f"were checked. This covers the containers only — cluster "
                                f"health, OSDs and disk failures come from query_ceph.")
                               + _coverage_note(client, window),
                    "dataframe": table, "raw": {"checked": total_non_heartbeat}}

        summary = (f"{len(rows)} distinct problem(s) reported by Ceph's daemon containers "
                   f"on server01 over {window}, from {total_non_heartbeat:,} non-heartbeat "
                   f"line(s). ")
        summary += " ".join(
            f"[{r['daemon']}] {r['message'][:160]} (seen {r['occurrences']}x, "
            f"{r['first_seen']} to {r['last_seen']})." for r in rows[:4])
        summary += (" Occurrence counts are exact, taken over the whole window rather "
                    "than a sample. ")
        if ungrouped:
            summary += (f"{ungrouped:,} line(s) could not be grouped because they exceed "
                        f"the index's 256-character limit for grouping, so they are "
                        f"counted in the total but not itemised above. ")
        summary += ("These are container-level faults, which the Ceph CLUSTER can report "
                    "HEALTH_OK alongside — the two are independent. Cluster health, OSDs "
                    "and disk failures come from query_ceph, not from here.")
        return {"summary": summary + _coverage_note(client, window),
                "dataframe": table, "raw": {"distinct": len(seen),
                                            "checked": total_non_heartbeat,
                                            "ungrouped": ungrouped}}

    except Exception as exc:
        return {"summary": f"Could not read the Ceph daemon log index: {exc}",
                "dataframe": pd.DataFrame(), "raw": {"error": str(exc)}}
