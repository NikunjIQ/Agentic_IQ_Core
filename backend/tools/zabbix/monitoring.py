"""What is wrong: live problems, notification records, and trigger state."""

import time
from datetime import date

import pandas as pd

from tools.clients import SEVERITY_LABELS, TICKET_ID_TAG


def query_problems(rpc, host_filter=None, status_filter=None) -> dict:
    problems = rpc("problem.get", {
        "output": ["eventid", "objectid", "clock", "name", "severity", "acknowledged"],
        "selectTags": "extend",
        "recent": True,
        "sortfield": ["eventid"], "sortorder": "DESC",
    })

    # problem.get returns a triggerid, not a host — resolve the whole batch at once
    trigger_ids = list({str(p.get("objectid")) for p in problems if p.get("objectid")})
    trigger_hosts = {}
    if trigger_ids:
        trigs = rpc("trigger.get", {
            "triggerids": trigger_ids,
            "output": ["triggerid"],
            "selectHosts": ["hostid", "host", "name"],
        })
        trigger_hosts = {t["triggerid"]: t.get("hosts", []) for t in trigs}

    for p in problems:
        hosts = trigger_hosts.get(str(p.get("objectid")), [])
        p["host"] = ", ".join(h.get("name") or h.get("host", "") for h in hosts) or "(unknown)"
        p["severity_label"] = SEVERITY_LABELS.get(int(p.get("severity", 0) or 0),
                                                  p.get("severity"))
        tags = {t.get("tag"): t.get("value") for t in (p.get("tags") or [])}
        # the Zabbix->Zammad webhook writes the created ticket id back here
        p["zammad_ticket_id"] = tags.get(TICKET_ID_TAG)
        p["has_ticket"] = bool(p["zammad_ticket_id"])
        p["tags"] = str(p.get("tags"))

    df = pd.DataFrame(problems)
    summary = "There are 0 active problems right now."
    if not df.empty:
        if host_filter:
            df = df[df["host"].str.contains(host_filter, case=False, na=False)]
        if status_filter and status_filter.isdigit():
            df = df[df["severity"].astype(int) >= int(status_filter)]

        if df.empty:
            summary = f"No active problems found matching '{host_filter or status_filter}'."
        else:
            with_ticket = int(df["has_ticket"].sum())
            scope = f" on hosts matching '{host_filter}'" if host_filter else ""
            summary = (f"There are currently {len(df)} active/recent problems{scope}. "
                       f"{with_ticket} already have a Zammad ticket, "
                       f"{len(df) - with_ticket} do not. ")
            details = [f"{r['host']}: {r['name']} ({r['severity_label']})"
                       for _, r in df.head(8).iterrows()]
            summary += "Problems: " + " | ".join(details)

    return {"summary": summary, "dataframe": df,
            "raw": {"problems": df.to_dict("records") if not df.empty else []}}


def query_alerts(rpc, date_filter=None) -> dict:
    params = {"output": "extend", "sortfield": "clock", "sortorder": "DESC"}
    today_scoped = bool(date_filter and "today" in date_filter.lower())
    if today_scoped:
        params["time_from"] = int(time.mktime(date.today().timetuple()))

    alerts = rpc("alert.get", params)
    df = pd.DataFrame(alerts)
    # Scope and the problems/alerts caveat belong on the empty answer too —
    # "0 alerts today" is a different statement from "0 alerts ever".
    scope = "sent today" if today_scoped else "in total (all time)"
    if not df.empty:
        df['clock'] = pd.to_numeric(df['clock'], errors='coerce')
        df['clock_dt'] = pd.to_datetime(df['clock'], unit='s')

        summary = f"Alerts {scope}: {len(alerts)}. "

        # An alert is a notification Zabbix tried to deliver, not a live
        # condition. Without this breakdown "how many alerts are active" gets
        # answered with the all-time total.
        if 'status' in df.columns:
            delivered = int((df['status'].astype(str) == '1').sum())
            failed = int((df['status'].astype(str) == '2').sum())
            pending = int((df['status'].astype(str) == '0').sum())
            summary += f"Delivered: {delivered}. Failed: {failed}. Pending: {pending}. "

        today_count = int((df['clock_dt'].dt.date == pd.Timestamp.utcnow().date()).sum())
        summary += (f"Sent today: {today_count}. "
                    f"NOTE: alerts are notification records, not live conditions — "
                    f"for what is wrong right now use query_type='problems'. ")
        details = [f"{a.get('subject', '')}" for a in alerts[:5]]
        summary += "Most recent: " + " | ".join(details)
    else:
        summary = (f"Alerts {scope}: 0. No notifications matched. "
                   f"NOTE: alerts are notification records, not live conditions — "
                   f"zero alerts does NOT mean nothing is wrong. For current problems "
                   f"use query_type='problems'.")

    return {"summary": summary, "dataframe": df, "raw": {"alerts": alerts}}


def query_triggers(rpc) -> dict:
    triggers = rpc("trigger.get", {"output": ["triggerid", "description", "status"]})
    df = pd.DataFrame(triggers)
    enabled = sum(1 for t in triggers if str(t.get('status')) == '0')
    disabled = sum(1 for t in triggers if str(t.get('status')) == '1')
    summary = f"Total triggers: {len(triggers)}. Enabled: {enabled}. Disabled: {disabled}."
    return {"summary": summary, "dataframe": df, "raw": {"triggers": triggers}}
