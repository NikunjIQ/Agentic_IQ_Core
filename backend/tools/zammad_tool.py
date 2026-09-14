import concurrent.futures
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

import pandas as pd
from langchain_core.tools import tool

from tools.clients import ZammadClient

# Which timestamp a date question is about. "Closed in the last 10 days" and
# "created in the last 10 days" are different questions over the same tickets.
_DATE_FIELDS = {
    "created": "created_at_dt",
    "opened": "created_at_dt",
    "updated": "updated_at_dt",
    "closed": "close_at_dt",
    "resolved": "close_at_dt",
}

# Anything the caller might want a breakdown by. Adding a dimension here makes
# every "which X has the most tickets" question answerable without new code.
_GROUP_DIMENSIONS = {
    "state": "state_name",
    "owner": "owner_name",
    "agent": "owner_name",
    "priority": "priority_name",
    "organization": "organization_name",
    "customer": "customer_email",
    "group": "group_name",
    "day": "created_date",
    # 'tag' is handled separately: tags are not a ticket column, so the
    # breakdown comes from Zammad's tag index rather than from the dataframe.
}

# Minutes are how Zammad stores SLA targets; hours read better past a point.
def _target_label(minutes) -> str:
    try:
        value = int(minutes)
    except (TypeError, ValueError):
        return "not set"
    if value < 60:
        return f"{value}m"
    if value % 60 == 0 and value < 1440:
        return f"{value // 60}h"
    return f"{value // 60}h {value % 60}m" if value < 1440 else f"{value / 1440:.1f}d"


# The team works in India, so "today" starts at midnight IST, not UTC — which
# otherwise still meant yesterday until 05:30.
IST = timezone(timedelta(hours=5, minutes=30))

# Raw tickets returned for the debug panel. Every ticket in the estate is ~5.7 MB.
RAW_ROWS = 500


def _window(date_filter: str, now: datetime) -> Optional[tuple]:
    """(start, end) for a relative date phrase, end None meaning "until now".

    None for a phrase this does not understand, so the caller applies no filter
    rather than a wrong one.
    """
    text = (date_filter or "").lower()
    today = now.date()

    def midnight(day):
        return datetime.combine(day, datetime.min.time(), IST)

    if "today" in text:
        return midnight(today), None
    if "yesterday" in text:
        return midnight(today - timedelta(days=1)), midnight(today)
    if "this month" in text:
        return midnight(today.replace(day=1)), None
    if "this week" in text:
        return midnight(today - timedelta(days=today.weekday())), None

    match = re.search(r"(\d+)\s*day", text)
    if match:
        return now - timedelta(days=int(match.group(1))), None
    return None


def _range_query(field: str, window: tuple) -> str:
    """Zammad search syntax for a window. Only the [a TO b] form works: the
    `field:>=value` form returns zero matches, with no error, on this server."""
    start, end = (f"{t.astimezone(timezone.utc):%Y-%m-%dT%H:%M:%SZ}" if t else "*"
                  for t in window)
    return f"{field}:[{start} TO {end}]"


def _quote(value: str) -> str:
    return '"' + str(value).replace('"', '\\"') + '"'


def _absent_note(search: str) -> str:
    # An empty text search is a real finding, and the reason matters: nothing in
    # this helpdesk is created by Ceph or Prometheus.
    return (f" NOTE: no ticket mentions '{search}'. Tickets here are raised "
            f"automatically only by Zabbix; Prometheus and Ceph have no "
            f"ticket integration, so an alert from those systems would "
            f"never create one. This is a genuine absence, not a gap in "
            f"the search.")


def _within_days(series: pd.Series, days: int, now: datetime) -> pd.Series:
    """Rows whose timestamp falls inside the last N days.

    The summary and the filters must agree: counting by whole days elapsed made
    "opened in last 10 days" report 130 while the same filter returned 127.
    """
    return series >= (now - timedelta(days=days))


def _match_any(series: pd.Series, needle: str) -> pd.Series:
    """Case-insensitive contains, tolerant of a comma-separated list."""
    wanted = [w.strip().lower() for w in str(needle).split(",") if w.strip()]
    text = series.fillna("").astype(str).str.lower()
    mask = pd.Series(False, index=series.index)
    for term in wanted:
        mask |= text.str.contains(re.escape(term), na=False)
    return mask


@tool
def query_zammad(
    search: Optional[str] = None,
    state_filter: Optional[str] = None,
    date_filter: Optional[str] = None,
    date_field: Optional[str] = None,
    owner_filter: Optional[str] = None,
    customer_filter: Optional[str] = None,
    organization_filter: Optional[str] = None,
    priority_filter: Optional[str] = None,
    group_by: Optional[str] = None,
    tag_filter: Optional[str] = None,
    overview: Optional[str] = None,
    ticket_reference: Optional[str] = None,
) -> dict:
    """
    Query the Zammad helpdesk: tickets, states, agents, organisations, priorities,
    tags, SLA performance.

    The summary ALWAYS carries estate-wide statistics (counts by state and priority,
    created/updated today, opened and closed in the last 10 days, unassigned, busiest
    agent, average first response, SLA attainment, largest tags), so many questions
    need no filter at all. Filters narrow; group_by ranks.

    - state_filter: state name(s), comma separated. Real states: new, open, closed,
      pending reminder, pending close, merged, 'Work in Progress', 'Escalated', 'On Hold'.
      If the user NAMES a state, pass that exact state only. Use the wider set
      'open,new,Work in Progress,Escalated' ONLY for a generic ask about active or
      unresolved tickets.
    - date_filter: ONLY set this when the user names a time period. A window nobody
      asked for silently answers a narrower question than the one posed. Accepted: a
      day, a calendar period, a rolling window of N days, or an exact date.
    - date_field: which timestamp date_filter applies to — 'created' (default),
      'updated', 'closed'. Use 'closed' for "closed/resolved in last N days".
    - owner_filter: assigned agent ("handled by X"). customer_filter: requester
      ("created by X"). organization_filter: organisation. priority_filter: e.g. '2'.
    - group_by: ranked breakdown — state, owner (or agent), priority, organization,
      customer, group, day, tag. Use for any "which X has the most tickets".
    - tag_filter: one or more tags, comma separated, ALL of which must be present.
      Tags are a real taxonomy here: 'class: os', 'component: cpu', 'scope: capacity',
      'target: windows', 'host: 192.168.1.73'. Use for "tickets about storage",
      "tickets for <device>". Resolved server-side, so it does not need the estate.
    - overview: a named Zammad queue, e.g. 'Unassigned & Open Tickets', 'Escalated
      Tickets', 'Work in progress', 'ON Hold'. Use when the question names a queue
      rather than a state.
    - ticket_reference: a ticket number or id. Returns that ticket's conversation
      (articles) and any time booked against it.
    - search: free text to find in ticket titles and bodies, e.g. 'ceph', 'disk full',
      'BGP'. Use this whenever the question asks whether a ticket EXISTS about some
      subject. Without it the other filters narrow by state or date only and the
      answer would describe unrelated tickets.

    WHERE TICKETS COME FROM: only Zabbix raises tickets automatically, via a webhook.
    Prometheus has no alerting rules and Ceph has no ticket integration, so an alert
    from either of those creates NO ticket. Finding nothing for a Ceph or Prometheus
    alert means no integration exists, not that the alert was handled.
    """
    client = ZammadClient()
    now = datetime.now(IST)
    today = now.date()
    date_column = _DATE_FIELDS.get((date_field or "created").lower(), "created_at_dt")
    window = _window(date_filter, now) if date_filter else None

    state_names = [s.strip() for s in (state_filter or "").split(",") if s.strip()]
    tags = [t.strip() for t in str(tag_filter or "").split(",") if t.strip()]

    # Filters Zammad applies itself, each checked against the full download by a
    # live test (test_zammad_server_filters_match_the_estate). Owner, priority,
    # customer and organisation are not here: priority.name returned 0 with no
    # error and owner.email an unrelated count, so they stay in pandas below.
    clauses, pushed = [], []
    if window:
        clauses.append(_range_query(date_column[:-3], window))
        pushed.append(f"{date_column[:-6]} within '{date_filter}' (IST)")
    if state_names:
        clauses.append("(" + " OR ".join(f"state.name:{_quote(s)}" for s in state_names) + ")")
        pushed.append(f"state in {[s.lower() for s in state_names]}")
    if search:
        clauses.append(f"({search})")
        pushed.append(f"text ~ '{search}'")
    if tags:
        clauses += [f"tags:{_quote(t)}" for t in tags]
        pushed.append(f"tags include {tags}")

    # A filtered question fetches only its matches (~1-6s) instead of the whole
    # estate (~14s). Too many matches, or a failed search, falls back to the
    # estate, and a failure never reads as "no tickets".
    tickets, scoped = None, False
    if clauses and not ticket_reference:
        try:
            tickets = client.search_all(" AND ".join(clauses))
            scoped = tickets is not None
        except Exception:
            tickets = None
    if tickets is None:
        tickets = client.all_tickets()

    if scoped and not tickets:
        summary = (f"Current date: {today.isoformat()} (IST). FILTERED RESULT: "
                   f"0 ticket(s) match [{'; '.join(pushed)}].")
        if search and len(clauses) == 1:
            summary += _absent_note(search)
        return {"summary": summary, "dataframe": pd.DataFrame(), "raw": {"result": []}}

    if not tickets:
        return {"summary": "Retrieved 0 tickets from Zammad or the server returned an error.",
                "dataframe": pd.DataFrame(), "raw": {"result": []}}

    # Nine small lookups of ~0.5s each. One after another they were most of a
    # filtered question's time; in parallel they cost about the slowest one.
    with concurrent.futures.ThreadPoolExecutor(max_workers=9) as pool:
        lookups = {name: pool.submit(fn) for name, fn in {
            "users": lambda: client.get("/api/v1/users", {"per_page": 200}) or [],
            "states": client.state_map, "priorities": client.priority_map,
            "orgs": client.organization_map, "groups": client.group_map,
            "slas": client.slas, "overviews": client.overviews,
            "tag_counts": client.tag_counts,
            # Booked time is estate-wide by construction; beside a scoped fetch's
            # figures it would read as belonging to them.
            "entries": (lambda: []) if scoped else client.time_accountings,
        }.items()}

    def looked_up(name):
        try:
            return lookups[name].result()
        except Exception:
            return {}

    users = {}
    for u in looked_up("users") or []:
        if "id" in u:
            label = u.get("email") or f"{u.get('firstname', '')} {u.get('lastname', '')}".strip()
            users[int(u["id"])] = label or str(u["id"])

    states = looked_up("states")
    priorities = looked_up("priorities")
    orgs = looked_up("orgs")
    groups = looked_up("groups")

    df = pd.DataFrame(tickets)

    def label_from(column, mapping, default=""):
        if column in df.columns:
            return df[column].map(
                lambda v: mapping.get(int(v), str(v)) if pd.notnull(v) else default)
        return pd.Series([default] * len(df), index=df.index)

    df["state_name"] = label_from("state_id", states)
    df["priority_name"] = label_from("priority_id", priorities)
    df["organization_name"] = label_from("organization_id", orgs, "(none)")
    df["group_name"] = label_from("group_id", groups)
    df["customer_email"] = label_from("customer_id", users)
    df["created_by_email"] = label_from("created_by_id", users)
    if "owner_id" in df.columns:
        df["owner_name"] = df["owner_id"].map(
            lambda v: users.get(int(v), "Unassigned") if pd.notnull(v) and int(v) > 1 else "Unassigned")
    else:
        df["owner_name"] = "Unassigned"

    for column in ("created_at", "updated_at", "close_at", "first_response_at"):
        if column in df.columns:
            # In IST, so every .dt.date below — "created today", "this month" — is
            # the team's calendar day.
            df[column + "_dt"] = pd.to_datetime(df[column], errors="coerce",
                                                utc=True).dt.tz_convert(IST)

    if "created_at_dt" in df.columns:
        df = df.sort_values("created_at_dt", ascending=False)
        df["created_date"] = df["created_at_dt"].dt.date
        df["days_since_created"] = (now - df["created_at_dt"]).dt.days
    if "updated_at_dt" in df.columns:
        df["days_since_updated"] = (now - df["updated_at_dt"]).dt.days
    if "first_response_at_dt" in df.columns and "created_at_dt" in df.columns:
        df["first_response_minutes"] = (
            df["first_response_at_dt"] - df["created_at_dt"]).dt.total_seconds() / 60.0

    # Zammad records how far inside (positive) or past (negative) the SLA target
    # each ticket landed, so attainment does not have to be recomputed.
    for source, flag in (("first_response_diff_in_min", "sla_first_response_met"),
                         ("close_diff_in_min", "sla_close_met")):
        if source in df.columns:
            numeric = pd.to_numeric(df[source], errors="coerce")
            df[flag] = numeric.where(numeric.isna(), numeric >= 0)

    # ---------------- estate-wide summary ----------------
    parts = [f"Current date: {today.isoformat()} (IST)."]
    if scoped:
        # Fetched for the filters alone, so every figure below covers only these.
        parts.append(f"Tickets matching [{'; '.join(pushed)}]: {len(df)} — the "
                     f"statistics below cover only these, not the whole helpdesk.")
    else:
        parts.append(f"Total tickets: {len(df)}.")
    parts.append(f"By state: {df['state_name'].value_counts().to_dict()}.")
    parts.append(f"By priority: {df['priority_name'].value_counts().to_dict()}.")

    if "created_at_dt" in df.columns:
        parts.append(f"Created today: {int((df['created_date'] == today).sum())}. "
                     f"Created yesterday: {int((df['created_date'] == today - timedelta(days=1)).sum())}. "
                     f"Opened in last 10 days: {int(_within_days(df['created_at_dt'], 10, now).sum())}.")
        this_month = df[(df['created_at_dt'].dt.month == today.month)
                        & (df['created_at_dt'].dt.year == today.year)]
        parts.append(f"Created this month: {len(this_month)} "
                     f"(average {len(this_month) / today.day:.1f} per day over {today.day} days).")

    if "updated_at_dt" in df.columns:
        parts.append(f"Updated today: {int((df['updated_at_dt'].dt.date == today).sum())}. "
                     f"No update in last 10 days: "
                     f"{int((~_within_days(df['updated_at_dt'], 10, now)).sum())}.")

    if "close_at_dt" in df.columns:
        parts.append(f"Closed today: {int((df['close_at_dt'].dt.date == today).sum())}. "
                     f"Closed in last 10 days: "
                     f"{int(_within_days(df['close_at_dt'], 10, now).fillna(False).sum())}.")

    unassigned = int((df["owner_name"] == "Unassigned").sum())
    parts.append(f"Unassigned tickets: {unassigned}.")

    assigned = df[df["owner_name"] != "Unassigned"]
    if not assigned.empty:
        busiest = assigned["owner_name"].value_counts()
        parts.append(f"Busiest agent by tickets held: {busiest.index[0]} ({int(busiest.iloc[0])}).")
        resolved = assigned[assigned["state_name"].str.lower() == "closed"]
        if not resolved.empty:
            top_resolver = resolved["owner_name"].value_counts()
            parts.append(f"Agent with most resolved (closed) tickets: {top_resolver.index[0]} "
                         f"({int(top_resolver.iloc[0])}). Tickets per agent (closed): "
                         f"{top_resolver.head(5).to_dict()}.")

    if "first_response_minutes" in df.columns:
        responded = df["first_response_minutes"].dropna()
        if not responded.empty:
            parts.append(f"Average first response time: {responded.mean():.1f} minutes "
                         f"(median {responded.median():.1f}, measured on {len(responded)} tickets).")

    # What the targets actually are. Attainment without the target is half an
    # answer: "96% within SLA" says nothing about which SLA or how demanding it is.
    sla_definitions = looked_up("slas")
    if sla_definitions:
        described = "; ".join(
            f"'{s.get('name')}' (first response {_target_label(s.get('first_response_time'))}, "
            f"update {_target_label(s.get('update_time'))}, "
            f"resolution {_target_label(s.get('solution_time'))})"
            for s in sla_definitions)
        parts.append(f"SLA policies configured: {len(sla_definitions)} — {described}.")

    for flag, label in (("sla_first_response_met", "First response SLA"),
                        ("sla_close_met", "Resolution SLA")):
        if flag in df.columns:
            measured = df[flag].dropna()
            if not measured.empty:
                met = int(measured.sum())
                # The percentage is over tickets Zammad actually scored, which is
                # a small slice of the estate. Stating the coverage stops "96%
                # within SLA" reading as an estate-wide health figure.
                coverage = len(measured) / len(df) * 100
                parts.append(f"{label}: {met} met, {len(measured) - met} breached, "
                             f"of {len(measured)} tickets with an SLA target "
                             f"({met / len(measured) * 100:.0f}% within SLA). "
                             f"NOTE: that covers {len(measured)} of {len(df)} tickets "
                             f"({coverage:.1f}% of the estate) — the rest were never "
                             f"scored against an SLA.")

    # Booked time. The ticket payload carries time_unit, but the accounting
    # endpoint is what attributes it, so totals come from there.
    entries = looked_up("entries")
    if entries:
        booked = 0.0
        per_ticket = {}
        for entry in entries:
            try:
                units = float(entry.get("time_unit") or 0)
            except (TypeError, ValueError):
                continue
            booked += units
            key = entry.get("ticket_id")
            per_ticket[key] = per_ticket.get(key, 0.0) + units
        if per_ticket:
            busiest = max(per_ticket.items(), key=lambda kv: kv[1])
            parts.append(f"Time booked: {booked:.1f} unit(s) across {len(entries)} entries "
                         f"on {len(per_ticket)} ticket(s) — most on ticket id {busiest[0]} "
                         f"({busiest[1]:.1f}). Time is recorded on "
                         f"{len(per_ticket)} of {len(df)} tickets.")

    queues = looked_up("overviews")
    if queues:
        parts.append(f"Named queues (overviews) available: "
                     f"{', '.join(repr(o.get('name')) for o in queues[:12])}.")

    org_counts = df[df["organization_name"] != "(none)"]["organization_name"].value_counts()
    if not org_counts.empty:
        parts.append(f"By organization: {org_counts.head(5).to_dict()} "
                     f"({int((df['organization_name'] == '(none)').sum())} have no organization).")

    tag_counts = looked_up("tag_counts")
    # Estate-wide, like booked time: not stated beside a scoped fetch's figures.
    if tag_counts and not scoped:
        top_tags = ", ".join(f"{t['tag']} ({t['tickets']})" for t in tag_counts[:8])
        parts.append(f"Total tags in use: {len(tag_counts)}. Largest tags: {top_tags}.")

    summary = " ".join(parts)

    # ---------------- one ticket's conversation ----------------
    # Answered before the estate filters, because "what was said on #904298" is a
    # question about one ticket rather than a narrowing of the whole estate.
    if ticket_reference:
        reference = str(ticket_reference).strip().lstrip("#")
        match = df[df["number"].astype(str) == reference] if "number" in df.columns else df.head(0)
        if match.empty and "id" in df.columns:
            match = df[df["id"].astype(str) == reference]
        if match.empty:
            return {"summary": f"No ticket found matching '{ticket_reference}'.",
                    "dataframe": pd.DataFrame(), "raw": {}}

        row = match.iloc[0]
        ticket_id = row.get("id")
        articles = client.articles(ticket_id)
        tags = client.ticket_tags(ticket_id)
        booked = [e for e in client.time_accountings()
                  if str(e.get("ticket_id")) == str(ticket_id)]
        spent = sum(float(e.get("time_unit") or 0) for e in booked)

        detail = [f"Ticket #{row.get('number')} — {row.get('title')!r}. "
                  f"State: {row.get('state_name')}. Priority: {row.get('priority_name')}. "
                  f"Owner: {row.get('owner_name')}. Customer: {row.get('customer_email')}."]
        if tags:
            detail.append(f"Tags: {', '.join(tags)}.")
        detail.append(f"Time booked: {spent:.1f} unit(s) across {len(booked)} entry(ies)."
                      if booked else "No time has been booked against this ticket.")
        detail.append(f"Conversation: {len(articles)} article(s).")

        rows = []
        for article in articles:
            body = " ".join(str(article.get("body") or "").split())
            # Zammad stores HTML for email articles; the tags carry no meaning
            # for a reader and swamp the summary.
            body = re.sub(r"<[^>]+>", " ", body)
            body = " ".join(body.split())
            rows.append({"from": article.get("from"), "type": article.get("type"),
                         "internal": article.get("internal"),
                         "created_at": article.get("created_at"),
                         "body": body[:400]})
        for article in rows[:6]:
            detail.append(f"[{article['type']}] {article['from']}: {article['body'][:220]}")

        return {"summary": " ".join(detail),
                "dataframe": pd.DataFrame(rows),
                "raw": {"ticket": row.to_dict(), "articles": articles,
                        "tags": tags, "time_accounting": booked}}

    # ---------------- filters ----------------
    output = df
    # A scoped fetch already applied these on the server. Text and tags cannot be
    # checked from a ticket row, so they only run here after an estate download;
    # state and date are re-applied below either way, which keeps them exact.
    applied = list(pushed) if scoped else []

    if search and not scoped:
        # Free text is resolved server-side. Without this the tool could only
        # narrow by state or date, so "is there a ticket about X" was answered
        # with a count of tickets that had nothing to do with X.
        matched = client.search(str(search), limit=ZammadClient.SEARCH_CAP)
        ids = {str(t.get("id")) for t in matched if t.get("id") is not None}
        if "id" in output.columns:
            output = output[output["id"].astype(str).isin(ids)]
        applied.append(f"text ~ '{search}'")
        if len(matched) >= ZammadClient.SEARCH_CAP:
            summary += (f" IMPORTANT: Zammad's search returns at most "
                        f"{ZammadClient.SEARCH_CAP} tickets and that ceiling was "
                        f"reached, so the count below is a MINIMUM.")
        elif not ids:
            summary += _absent_note(search)

    if tags:
        wanted = tags
        matched = [] if scoped else client.tickets_by_tags(wanted)
        if not scoped:
            ids = {str(t.get("id")) for t in matched if t.get("id") is not None}
            if "id" in output.columns:
                output = output[output["id"].astype(str).isin(ids)]
            applied.append(f"tags include {wanted}")

        # Zammad's tag index is the authoritative count. Search returned 82 for a
        # tag the index counts at 83, so both are reported rather than picking one
        # and calling it the total.
        if len(wanted) == 1:
            indexed = next((t["tickets"] for t in tag_counts
                            if t["tag"].lower() == wanted[0].lower()), None)
            if indexed is not None and indexed != len(output):
                summary += (f" NOTE: Zammad's tag index counts {indexed} ticket(s) "
                            f"tagged '{wanted[0]}'; search returned {len(output)}. "
                            f"Treat {indexed} as the total.")

        # Search stops at a fixed ceiling however many rows are asked for, so a
        # result sitting exactly on it is a floor, not a count.
        if len(matched) >= ZammadClient.SEARCH_CAP:
            total = None
            if len(wanted) == 1:
                total = next((t["tickets"] for t in tag_counts
                              if t["tag"].lower() == wanted[0].lower()), None)
            summary += (
                f" IMPORTANT: Zammad's search returns at most "
                f"{ZammadClient.SEARCH_CAP} tickets, and that ceiling was reached, "
                f"so the filtered count below is a MINIMUM, not a total."
                + (f" Zammad's tag index counts {total} ticket(s) with this tag."
                   if total is not None else
                   " No exact total is available for a multi-tag query."))

    if overview:
        queue = next((o for o in (queues or [])
                      if str(o.get("name", "")).lower() == str(overview).strip().lower()), None)
        if queue is None:
            queue = next((o for o in (queues or [])
                          if str(overview).strip().lower() in str(o.get("name", "")).lower()), None)
        if queue is None:
            available = ", ".join(repr(o.get("name")) for o in (queues or []))
            return {"summary": f"No Zammad queue named '{overview}'. Available: {available}.",
                    "dataframe": pd.DataFrame(), "raw": {"overviews": queues}}
        ids = {str(i) for i in client.overview_ticket_ids(queue.get("link"))}
        if "id" in output.columns:
            output = output[output["id"].astype(str).isin(ids)]
        applied.append(f"in queue '{queue.get('name')}'")

    if state_names:
        wanted = [s.lower() for s in state_names]
        output = output[output["state_name"].str.lower().isin(wanted)]
        if not scoped:
            applied.append(f"state in {wanted}")
    if priority_filter:
        needle = str(priority_filter).strip()
        if needle.isdigit():
            needle = f"priority {needle}"
        output = output[_match_any(output["priority_name"], needle)]
        applied.append(f"priority ~ '{needle}'")
    if owner_filter:
        output = output[_match_any(output["owner_name"], owner_filter)]
        applied.append(f"owner ~ '{owner_filter}'")
    if customer_filter:
        output = output[_match_any(output["customer_email"], customer_filter)
                        | _match_any(output["created_by_email"], customer_filter)]
        applied.append(f"customer ~ '{customer_filter}'")
    if organization_filter:
        output = output[_match_any(output["organization_name"], organization_filter)]
        applied.append(f"organization ~ '{organization_filter}'")
    if window and date_column in output.columns:
        # Applied even to a scoped fetch: search bounds are inclusive, this is exact.
        start, end = window
        mask = output[date_column] >= start
        if end is not None:
            mask &= output[date_column] < end
        output = output[mask.fillna(False)]
        if not scoped:
            applied.append(f"{date_column[:-6]} within '{date_filter}' (IST)")

    # Only the tickets that matched, capped — never the whole estate.
    if "id" in output.columns:
        kept = set(output["id"].astype(str))
        raw = {"result": [t for t in tickets if str(t.get("id")) in kept][:RAW_ROWS],
               "matched": len(output)}
    else:
        raw = {"result": tickets[:RAW_ROWS], "matched": len(tickets)}

    if applied:
        summary += (f" FILTERED RESULT: {len(output)} ticket(s) match "
                    f"[{'; '.join(applied)}].")
        if 0 < len(output) <= 15:
            listing = ", ".join(
                f"#{r.get('number')} '{str(r.get('title'))[:40]}' ({r.get('state_name')}, "
                f"owner {r.get('owner_name')})" for _, r in output.iterrows())
            summary += f" Matching tickets: {listing}."

    # ---------------- breakdown ----------------
    if group_by and group_by.strip().lower() in ("tag", "tags"):
        # Tags are not a ticket column, so this breakdown comes from Zammad's tag
        # index. It is estate-wide by construction and cannot be combined with
        # the filters above — saying so beats returning a number that looks
        # filtered and is not.
        rows = [{"tag": t["tag"], "tickets": t["tickets"]} for t in tag_counts]
        table = pd.DataFrame(rows)
        top = ", ".join(f"{r['tag']}: {r['tickets']}" for r in rows[:12])
        summary += (f" BREAKDOWN BY TAG ({len(rows)} distinct tags, highest first): {top}.")
        if applied:
            summary += (" NOTE: this tag breakdown is estate-wide — tags live outside "
                        "the ticket record, so it does not reflect the filters above.")
        return {"summary": summary, "dataframe": table, "raw": {"tags": tag_counts}}

    if group_by:
        column = _GROUP_DIMENSIONS.get(group_by.strip().lower())
        if column and column in output.columns:
            counts = output[column].value_counts()
            output = counts.rename_axis(column).reset_index(name="tickets")
            top = ", ".join(f"{r[column]}: {r['tickets']}" for _, r in output.head(10).iterrows())
            summary += (f" BREAKDOWN BY {group_by.upper()} ({len(output)} distinct values, "
                        f"highest first): {top}.")
        else:
            summary += (f" Cannot group by '{group_by}'. Supported: "
                        f"{sorted(set(_GROUP_DIMENSIONS) | {'tag'})}.")

        return {"summary": summary, "dataframe": output, "raw": raw}

    # A raw ticket carries ~45 columns of ids, escalation stamps and preferences.
    # Previewing ten of those wastes most of the prompt on values nobody reads,
    # so hand back only the columns a person would actually look at.
    display_columns = [c for c in (
        "number", "title", "state_name", "priority_name", "owner_name",
        "customer_email", "organization_name", "created_at_dt", "updated_at_dt",
        "close_at_dt", "first_response_minutes", "sla_first_response_met",
    ) if c in output.columns]
    if display_columns:
        output = output[display_columns]

    return {"summary": summary, "dataframe": output, "raw": raw}
