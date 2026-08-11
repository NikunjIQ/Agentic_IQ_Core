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
}


def _window_mask(df: pd.DataFrame, column: str, date_filter: str) -> Optional[pd.Series]:
    """Translate a relative date phrase into a mask over one timestamp column."""
    if column not in df.columns or not date_filter:
        return None

    text = date_filter.lower()
    now = datetime.now(timezone.utc)
    today = now.date()
    series = df[column]

    if "today" in text:
        return series.dt.date == today
    if "yesterday" in text:
        return series.dt.date == (today - timedelta(days=1))
    if "this month" in text:
        return (series.dt.month == today.month) & (series.dt.year == today.year)
    if "this week" in text:
        return series.dt.date >= (today - timedelta(days=today.weekday()))

    match = re.search(r"(\d+)\s*day", text)
    if match:
        cutoff = now - timedelta(days=int(match.group(1)))
        return series >= cutoff
    return None


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
    question: Optional[str] = None,
    state_filter: Optional[str] = None,
    date_filter: Optional[str] = None,
    date_field: Optional[str] = None,
    owner_filter: Optional[str] = None,
    customer_filter: Optional[str] = None,
    organization_filter: Optional[str] = None,
    priority_filter: Optional[str] = None,
    group_by: Optional[str] = None,
) -> dict:
    """
    Query the Zammad IT helpdesk and ticketing system for ticket data, counts, states,
    agent workload, organizations, priorities, tags, and SLA performance.

    The summary ALWAYS includes estate-wide statistics (counts by state and priority,
    created/updated today, opened and closed in the last 10 days, unassigned count,
    busiest agent, average first response time, SLA attainment, tickets per day this
    month, and the largest tags), so many questions are answered without any filter.
    Use the filters to narrow, and group_by to rank.

    - state_filter: Ticket state name(s), comma separated. Real states are: new, open,
      closed, pending reminder, pending close, merged, 'Work in Progress', 'Escalated',
      'On Hold'.
      If the user NAMES a state ("in Open state", "Work in Progress", "escalated"), pass
      that exact state only.
      Pass the wider set 'open,new,Work in Progress,Escalated' ONLY when the user asks
      generically for active/unresolved/outstanding tickets without naming a state.
    - date_filter: Relative window, e.g. 'today', 'yesterday', 'this week', 'this month',
      'last 10 days'. OMIT THIS unless the user actually mentions a time period — adding
      a window they did not ask for silently answers a narrower question.
    - date_field: Which timestamp date_filter applies to — 'created' (default), 'updated',
      or 'closed'. Use 'closed' for "how many tickets were closed/resolved in last N days".
    - owner_filter: Assigned agent, by name or email. Use for "handled by <person>".
    - customer_filter: Requester, by name or email. Use for "created by <person>".
    - organization_filter: Organization name.
    - priority_filter: Priority name, e.g. 'priority 2' or just '2'.
    - group_by: Return a ranked breakdown instead of a flat list. One of: state, owner
      (or agent), priority, organization, customer, group, day. Use this for any
      "which X has the most tickets" question.

    Note: tags live outside the ticket record, so tag totals appear in the summary but
    cannot be combined with the filters above.
    """
    client = ZammadClient()
    tickets = client.all_tickets()

    if not tickets:
        return {"summary": "Retrieved 0 tickets from Zammad or the server returned an error.",
                "dataframe": pd.DataFrame(), "raw": {"result": []}}

    users = {}
    try:
        for u in (client.get("/api/v1/users", {"per_page": 200}) or []):
            if "id" in u:
                label = u.get("email") or f"{u.get('firstname', '')} {u.get('lastname', '')}".strip()
                users[int(u["id"])] = label or str(u["id"])
    except Exception:
        pass

    states = client.state_map()
    priorities = client.priority_map()
    orgs = client.organization_map()
    groups = client.group_map()

    df = pd.DataFrame(tickets)
    now = datetime.now(timezone.utc)
    today = now.date()

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
            df[column + "_dt"] = pd.to_datetime(df[column], errors="coerce", utc=True)

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
    parts = [f"Current date: {today.isoformat()}. Total tickets: {len(df)}."]
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

    for flag, label in (("sla_first_response_met", "First response SLA"),
                        ("sla_close_met", "Resolution SLA")):
        if flag in df.columns:
            measured = df[flag].dropna()
            if not measured.empty:
                met = int(measured.sum())
                parts.append(f"{label}: {met} met, {len(measured) - met} breached, "
                             f"of {len(measured)} tickets with an SLA target "
                             f"({met / len(measured) * 100:.0f}% within SLA).")

    org_counts = df[df["organization_name"] != "(none)"]["organization_name"].value_counts()
    if not org_counts.empty:
        parts.append(f"By organization: {org_counts.head(5).to_dict()} "
                     f"({int((df['organization_name'] == '(none)').sum())} have no organization).")

    tag_counts = client.tag_counts()
    if tag_counts:
        top_tags = ", ".join(f"{t['tag']} ({t['tickets']})" for t in tag_counts[:8])
        parts.append(f"Total tags in use: {len(tag_counts)}. Largest tags: {top_tags}.")

    summary = " ".join(parts)

    # ---------------- filters ----------------
    output = df
    applied = []

    if state_filter:
        wanted = [s.strip().lower() for s in state_filter.split(",") if s.strip()]
        output = output[output["state_name"].str.lower().isin(wanted)]
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
    if date_filter:
        column = _DATE_FIELDS.get((date_field or "created").lower(), "created_at_dt")
        mask = _window_mask(output, column, date_filter)
        if mask is not None:
            output = output[mask.fillna(False)]
            applied.append(f"{column.replace('_at_dt', '')} within '{date_filter}'")

    if applied:
        summary += (f" FILTERED RESULT: {len(output)} ticket(s) match "
                    f"[{'; '.join(applied)}].")
        if 0 < len(output) <= 15:
            listing = ", ".join(
                f"#{r.get('number')} '{str(r.get('title'))[:40]}' ({r.get('state_name')}, "
                f"owner {r.get('owner_name')})" for _, r in output.iterrows())
            summary += f" Matching tickets: {listing}."

    # ---------------- breakdown ----------------
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
                        f"{sorted(set(_GROUP_DIMENSIONS))}.")

        return {"summary": summary, "dataframe": output, "raw": {"result": tickets}}

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

    return {"summary": summary, "dataframe": output, "raw": {"result": tickets}}
