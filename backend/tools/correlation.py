"""
Deterministic correlation between Zabbix problems and Zammad tickets.

The join is not guesswork. The Zabbix->Zammad webhook writes the created ticket
id back onto the event as the tag `__zbx_zammad_ticket_id`, and stamps the event
id into the ticket body ("Original problem ID: 155672"). Either direction gives
an exact key, so the matching happens here in code and the LLM only narrates a
table that is already correct.

Rows that have no ticket are kept rather than dropped — "this is broken and
nobody raised a ticket" is usually the most useful answer in the table.
"""

import html
import re
from typing import Optional

import pandas as pd
from langchain_core.tools import tool

from tools.clients import (
    Cached,
    ZabbixClient,
    ZammadClient,
    SEVERITY_LABELS,
    TICKET_ID_TAG,
    TICKET_LINK_TAG,
)

# ---------------------------------------------------------------------------
# Ticket body parsing
# ---------------------------------------------------------------------------
# The webhook emits a fixed template, so these are exact reads, not heuristics.
_BODY_PATTERNS = {
    "event_id": re.compile(r"Original problem ID:\s*(\d+)", re.I),
    "problem_name": re.compile(r"Problem name:\s*(.+)", re.I),
    "host": re.compile(r"Host:\s*(.+)", re.I),
    # Only some Zabbix message templates carry it (Zammad 183's does, 205's does not).
    "ip": re.compile(r"IP Address:\s*(\d{1,3}(?:\.\d{1,3}){3})", re.I),
    "severity": re.compile(r"Severity:\s*(\w+)", re.I),
    "started_at": re.compile(r"Problem started at\s*([\d:]+)\s*on\s*([\d.]+)", re.I),
    "trigger_id": re.compile(r"triggerid=(\d+)", re.I),
    "url_event_id": re.compile(r"eventid=(\d+)", re.I),
}


# A body typed or pasted into Zammad's editor is HTML on one line, so "Host:
# (.+)" would run on through every later field. Line-ending tags become newlines.
_LINE_END_TAG = re.compile(r"<\s*(?:br|/div|/p|/li|/tr)\s*/?\s*>", re.I)
_ANY_TAG = re.compile(r"<[^>]+>")


def parse_zabbix_ticket_body(body: str) -> dict:
    """Pull Zabbix identifiers out of an auto-created ticket body."""
    out = {}
    if not body:
        return out
    if "<" in body:
        body = html.unescape(_ANY_TAG.sub("", _LINE_END_TAG.sub("\n", body)))
    for field, pattern in _BODY_PATTERNS.items():
        match = pattern.search(body)
        if not match:
            continue
        if field == "started_at":
            out["started_at"] = f"{match.group(2)} {match.group(1)}"
        else:
            out[field] = match.group(1).strip()
    # the URL is a second, independent source of the event id
    if "event_id" not in out and "url_event_id" in out:
        out["event_id"] = out["url_event_id"]
    return out


# ---------------------------------------------------------------------------
# Device index (11 hosts today, so a plain in-memory dict is plenty)
# ---------------------------------------------------------------------------
DEVICE_CACHE_TTL = 300
_DEVICE_CACHE = Cached("device index", DEVICE_CACHE_TTL)


def build_device_index(force: bool = False) -> dict:
    """
    hostid -> {hostid, host, name, ips, groups, aliases}

    `aliases` is every string this device might be referred to by: technical
    name, visible name, and each interface IP. Everything that needs to turn a
    fuzzy human reference into a real device goes through here.
    """
    if not force:
        cached = _DEVICE_CACHE.get()
        if cached:
            return cached

    index = {}
    try:
        for h in ZabbixClient().hosts():
            ips = [i.get("ip") for i in (h.get("interfaces") or []) if i.get("ip")]
            dns = [i.get("dns") for i in (h.get("interfaces") or []) if i.get("dns")]
            aliases = {a for a in ([h.get("host"), h.get("name")] + ips + dns) if a}
            index[h["hostid"]] = {
                "hostid": h["hostid"],
                "host": h.get("host"),
                "name": h.get("name"),
                "ips": ips,
                "groups": [g.get("name") for g in (h.get("hostgroups") or [])],
                "aliases": sorted(aliases),
            }
    except Exception:
        # Stale beats empty: a transient Zabbix failure must not wipe the index.
        return _DEVICE_CACHE.value or {}

    return _DEVICE_CACHE.put(index)


def resolve_device(query: str) -> Optional[dict]:
    """Turn 'the windows box', '192.168.1.73' or 'Nikunj VM' into one device."""
    if not query:
        return None
    index = build_device_index()
    q = query.strip().lower()

    for device in index.values():                       # exact alias
        if any(q == a.lower() for a in device["aliases"]):
            return device
    for device in index.values():                       # alias inside the query
        if any(a.lower() in q for a in device["aliases"] if len(a) > 3):
            return device
    for device in index.values():                       # query inside an alias
        if any(q in a.lower() for a in device["aliases"]):
            return device
    return None


# ---------------------------------------------------------------------------
# The correlator
# ---------------------------------------------------------------------------
def _compact_table(df: pd.DataFrame, columns: list, limit: int = 60) -> str:
    """Render the whole join compactly.

    The orchestrator only forwards `df.head(10)` to the model, so the complete
    result is embedded in the summary instead — a correlation answer that silently
    drops rows is worse than no answer.
    """
    if df.empty:
        return "(no rows)"
    present = [c for c in columns if c in df.columns]
    shown = df.head(limit)
    lines = [" | ".join(present)]
    for _, row in shown.iterrows():
        cells = []
        for c in present:
            value = row.get(c)
            cells.append("-" if pd.isna(value) else str(value)[:60])
        lines.append(" | ".join(cells))
    if len(df) > limit:
        lines.append(f"... ({len(df) - limit} more rows)")
    return "\n".join(lines)


def correlate(host_filter: Optional[str] = None,
              severity_min: Optional[int] = None,
              only_gaps: bool = False) -> dict:
    """Join active Zabbix problems to their Zammad tickets. Returns raw parts."""
    zbx = ZabbixClient()
    zam = ZammadClient()

    problems = zbx.problems()
    trigger_hosts = zbx.triggers_to_hosts([p.get("objectid") for p in problems])

    device = resolve_device(host_filter) if host_filter else None

    rows = []
    for p in problems:
        tags = {t.get("tag"): t.get("value") for t in (p.get("tags") or [])}
        hosts = trigger_hosts.get(str(p.get("objectid")), [])
        host_name = ", ".join(h.get("name") or h.get("host", "") for h in hosts) or "(unknown)"
        host_ids = [h.get("hostid") for h in hosts]

        if device and device["hostid"] not in host_ids:
            continue
        if host_filter and not device:
            # unresolvable reference — fall back to substring on the host name
            if host_filter.lower() not in host_name.lower():
                continue

        severity = int(p.get("severity", 0) or 0)
        if severity_min is not None and severity < severity_min:
            continue

        started = pd.to_datetime(int(p.get("clock", 0)), unit="s", utc=True)
        rows.append({
            "host": host_name,
            "severity": SEVERITY_LABELS.get(severity, severity),
            "severity_code": severity,
            "problem": p.get("name"),
            "started_at": started,
            "age_hours": round((pd.Timestamp.utcnow() - started).total_seconds() / 3600, 1),
            "acknowledged": str(p.get("acknowledged")) == "1",
            "event_id": p.get("eventid"),
            "ticket_id": tags.get(TICKET_ID_TAG),
            "ticket_url": tags.get(TICKET_LINK_TAG),
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return {"df": df, "problems": problems, "tickets": {}}

    # one batched fetch of exactly the tickets referenced by the tags
    tickets = zam.tickets_by_id([t for t in df["ticket_id"].dropna().unique()])
    states = zam.state_map()
    priorities = zam.priority_map()

    def enrich(row):
        tid = row["ticket_id"]
        # pandas coerces the missing ids in this column to NaN, and NaN is truthy —
        # testing `if not tid` alone sends every untagged problem down the "ticket
        # is missing from Zammad" branch instead of reporting it as a real gap.
        if pd.isna(tid) or not tid:
            row["link"] = "NO TICKET"
            row["evidence"] = "no __zbx_zammad_ticket_id tag on the Zabbix event"
            return row
        ticket = tickets.get(str(tid))
        if not ticket:
            row["link"] = "DANGLING"
            row["evidence"] = f"event tagged with ticket {tid}, but that ticket is gone from Zammad"
            return row
        row["ticket_number"] = ticket.get("number")
        row["ticket_title"] = ticket.get("title")
        row["ticket_state"] = states.get(int(ticket["state_id"])) if ticket.get("state_id") else None
        row["ticket_priority"] = priorities.get(int(ticket["priority_id"])) if ticket.get("priority_id") else None
        row["ticket_created_at"] = ticket.get("created_at")
        row["link"] = "EXACT"
        row["evidence"] = (f"Zabbix event {row['event_id']} tagged {TICKET_ID_TAG}={tid} "
                           f"-> Zammad ticket #{ticket.get('number')}")
        return row

    df = df.apply(enrich, axis=1)
    df = df.sort_values(["severity_code", "age_hours"], ascending=[False, False])
    if only_gaps:
        df = df[df["link"] == "NO TICKET"]
    return {"df": df, "problems": problems, "tickets": tickets}


# ---------------------------------------------------------------------------
# Tools exposed to the model
# ---------------------------------------------------------------------------
@tool
def correlate_problems_and_tickets(
    host_filter: Optional[str] = None,
    severity_min: Optional[int] = None,
    only_gaps: Optional[bool] = False,
) -> dict:
    """
    Correlate active Zabbix problems with their Zammad tickets in ONE call.

    Use this for ANY question that spans both monitoring and ticketing — for example
    "tickets for device X and what's wrong with it", "which problems have no ticket",
    "what is the main issue on 192.168.1.73 and is it being worked on".
    Do NOT call query_zabbix and query_zammad separately for such questions: this tool
    already performs the exact join and returns the answer table.

    Results are sorted with the most severe, longest-running problem first, so the
    top row is the "main problem".

    - host_filter: Optional. A device name or IP (e.g. '192.168.1.73', 'Nikunj VM').
    - severity_min: Optional. 0=Not classified 1=Information 2=Warning 3=Average
      4=High 5=Disaster. Pass 2 or higher to ignore low-level noise.
    - only_gaps: Optional. True returns ONLY problems that have no ticket raised.
    """
    try:
        result = correlate(host_filter, severity_min, bool(only_gaps))
    except Exception as e:
        return {"summary": f"Correlation failed: {e}", "dataframe": pd.DataFrame(), "raw": {"error": str(e)}}

    df = result["df"]
    if df.empty:
        scope = f" for '{host_filter}'" if host_filter else ""
        return {"summary": f"No active problems found{scope}.", "dataframe": df, "raw": {}}

    linked = int((df["link"] == "EXACT").sum())
    gaps = int((df["link"] == "NO TICKET").sum())
    dangling = int((df["link"] == "DANGLING").sum())

    parts = [
        f"Correlated {len(df)} active problem(s)"
        + (f" on '{host_filter}'" if host_filter else "")
        + f": {linked} with a linked Zammad ticket, {gaps} with NO ticket raised"
        + (f", {dangling} pointing at a deleted ticket." if dangling else "."),
        "Join is exact (Zabbix event tag -> Zammad ticket id), not inferred. "
        "The counts above are complete — trust them over any row listing below.",
    ]

    top = df.iloc[0]
    parts.append(
        f"Most significant problem: '{top['problem']}' on {top['host']} "
        f"(severity {top['severity']}, active {top['age_hours']}h, {top['link']})."
    )

    # Estates repeat the same problem across many interfaces or peers, so dumping
    # every row burns the context window on near-duplicates. Roll up per device and
    # spell out only the rows that carry distinct information.
    rollup = (df.groupby("host")
                .agg(problems=("problem", "size"),
                     worst=("severity", "first"),
                     linked=("link", lambda s: int((s == "EXACT").sum())),
                     no_ticket=("link", lambda s: int((s == "NO TICKET").sum())))
                .reset_index()
                .sort_values("problems", ascending=False))
    parts.append("\nBY DEVICE (complete):\n" + _compact_table(
        rollup, ["host", "problems", "worst", "linked", "no_ticket"], limit=40))

    linked_rows = df[df["link"] == "EXACT"]
    if not linked_rows.empty:
        parts.append("\nPROBLEMS WITH A LINKED TICKET (all of them):\n" + _compact_table(
            linked_rows, ["host", "severity", "problem", "age_hours",
                          "ticket_number", "ticket_state", "evidence"], limit=25))

    gap_rows = df[df["link"] == "NO TICKET"]
    if not gap_rows.empty:
        parts.append(f"\nPROBLEMS WITH NO TICKET ({len(gap_rows)} total, "
                     f"nothing was raised in Zammad for these):")
        if len(gap_rows) <= 15:
            parts.append(_compact_table(
                gap_rows, ["host", "severity", "problem", "age_hours"], limit=15))
        else:
            grouped = (gap_rows.groupby(["host", "severity"])
                               .agg(count=("problem", "size"),
                                    example=("problem", "first"))
                               .reset_index()
                               .sort_values("count", ascending=False))
            parts.append(_compact_table(
                grouped, ["host", "severity", "count", "example"], limit=25))

    return {"summary": "\n".join(parts), "dataframe": df,
            "raw": {"row_count": len(df), "linked": linked,
                    "gaps": gaps, "dangling": dangling}}


@tool
def lookup_ticket_context(ticket_reference: str) -> dict:
    """
    Given a Zammad ticket (id, number, or a phrase from its title), find which
    device and Zabbix problem it came from.

    This is the reverse direction of correlate_problems_and_tickets. Use it for
    questions like "what device is ticket #904125 about" or "which host caused
    this ticket". Works by reading the Zabbix identifiers stamped into the ticket
    body, and by consulting the Zabbix event tag index.
    """
    zam = ZammadClient()
    zbx = ZabbixClient()

    ref = str(ticket_reference).strip().lstrip("#")
    ticket = zam.ticket(ref) if ref.isdigit() else None
    if ticket is None:
        matches = zam.search(ref, limit=10)
        if not matches:
            return {"summary": f"No Zammad ticket matched '{ticket_reference}'.",
                    "dataframe": pd.DataFrame(), "raw": {}}
        ticket = matches[0]

    ticket_id = ticket.get("id")
    body = "\n".join(str(a.get("body") or "") for a in zam.articles(ticket_id))
    parsed = parse_zabbix_ticket_body(body)

    host_name = parsed.get("host")
    evidence = []
    if host_name:
        evidence.append(f"ticket body states 'Host: {host_name}'")
    if parsed.get("event_id"):
        evidence.append(f"ticket body states 'Original problem ID: {parsed['event_id']}'")

    # cross-check against the Zabbix side of the link
    if not host_name:
        event = zbx.events_with_ticket_tags().get(str(ticket_id))
        if event:
            hosts = zbx.triggers_to_hosts([event.get("objectid")]).get(str(event.get("objectid")), [])
            host_name = ", ".join(h.get("name") or h.get("host", "") for h in hosts) or None
            if host_name:
                evidence.append(f"Zabbix event {event.get('eventid')} is tagged with this ticket id")

    states = zam.state_map()
    state_name = states.get(int(ticket["state_id"])) if ticket.get("state_id") else None

    if not host_name:
        summary = (f"Ticket #{ticket.get('number')} ('{ticket.get('title')}') has no Zabbix "
                   f"identifiers in its body and no event is tagged with it — it was most "
                   f"likely raised by a person, not by monitoring.")
        device = None
    else:
        device = resolve_device(host_name)
        summary = (f"Ticket #{ticket.get('number')} ('{ticket.get('title')}', state {state_name}) "
                   f"originates from device '{host_name}'. "
                   f"Problem: {parsed.get('problem_name', 'unknown')}. "
                   f"Severity: {parsed.get('severity', 'unknown')}. "
                   f"Evidence: {'; '.join(evidence)}.")

    df = pd.DataFrame([{
        "ticket_number": ticket.get("number"),
        "ticket_id": ticket_id,
        "ticket_title": ticket.get("title"),
        "ticket_state": state_name,
        "device": host_name,
        "device_ips": ", ".join(device["ips"]) if device else "",
        "zabbix_event_id": parsed.get("event_id"),
        "zabbix_trigger_id": parsed.get("trigger_id"),
        "problem_name": parsed.get("problem_name"),
        "severity": parsed.get("severity"),
        "started_at": parsed.get("started_at"),
    }])

    return {"summary": summary, "dataframe": df,
            "raw": {"ticket": ticket, "parsed_body": parsed}}
