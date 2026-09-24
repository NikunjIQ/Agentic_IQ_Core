"""What changed: the configuration audit log for one device."""

import pandas as pd

# auditlog.get resource types and actions, for turning numeric codes into words.
AUDIT_RESOURCE_TYPES = {
    4: "host", 6: "graph", 13: "trigger", 15: "item",
    16: "image", 17: "value map", 19: "IT service", 30: "template",
}
AUDIT_ACTIONS = {
    0: "added", 1: "updated", 2: "deleted", 3: "logged in", 4: "logged out",
    5: "executed", 6: "failed login", 7: "history cleared",
    8: "enabled", 9: "disabled",
}


def resolve_resources(rpc, host_filter):
    """(matching hosts, {resource id: audit resource type}) for one device, or
    ([], {}) when no host matches.

    Resolve the device first. The audit log is dominated by automatic
    item/trigger edits, so scanning "the last 100 entries" and grepping for a
    hostname finds nothing — host changes are older than the background churn
    and are keyed by hostid, not by name.
    """
    matched_hosts = rpc("host.get", {
        "output": ["hostid", "host", "name"],
        "search": {"name": host_filter, "host": host_filter},
        "searchByAny": True,
    })
    if not matched_hosts:
        return [], {}

    host_ids = {str(h["hostid"]) for h in matched_hosts}
    expected_type = {rid: "4" for rid in host_ids}
    try:
        for item in rpc("item.get", {"output": ["itemid"],
                                     "hostids": list(host_ids), "limit": 500}):
            expected_type[str(item["itemid"])] = "15"
        for trig in rpc("trigger.get", {"output": ["triggerid"],
                                        "hostids": list(host_ids), "limit": 500}):
            expected_type[str(trig["triggerid"])] = "13"
    except Exception:
        pass  # host-level history alone still answers the question
    return matched_hosts, expected_type


def audit_entries(rpc, expected_type, time_from=None, time_till=None, limit=500) -> list:
    """This device's audit records, newest first, labelled.

    Asked for by id. Sorting the whole log by time and taking the newest N never
    reaches them — background item/trigger edits generate far more entries than
    host changes do. time_from/time_till (epoch seconds) ask for one window;
    without them the newest `limit` rows are all there is, which on a router is
    about a day.
    """
    params = {
        "output": "extend",
        "filter": {"resourceid": sorted(expected_type)},
        "sortfield": "clock", "sortorder": "DESC",
        "limit": limit,
    }
    if time_from is not None:
        params["time_from"] = int(time_from)
    if time_till is not None:
        params["time_till"] = int(time_till)

    relevant = []
    for entry in rpc("auditlog.get", params):
        rid = str(entry.get("resourceid"))
        # ids are only unique within a resource type, so confirm the pair
        if expected_type.get(rid) != str(entry.get("resourcetype")):
            continue
        entry["resource"] = AUDIT_RESOURCE_TYPES.get(int(entry.get("resourcetype", -1)), "other")
        entry["change"] = AUDIT_ACTIONS.get(int(entry.get("action", -1)), entry.get("action"))
        entry["details"] = str(entry.get("details"))[:200]
        relevant.append(entry)
    return relevant


def query_audit(rpc, host_filter=None) -> dict:
    if not host_filter:
        return {"summary": "Please specify a host_filter (device) to check config changes.",
                "dataframe": pd.DataFrame(), "raw": {}}

    matched_hosts, expected_type = resolve_resources(rpc, host_filter)
    if not matched_hosts:
        return {"summary": f"Could not find a host matching '{host_filter}'.",
                "dataframe": pd.DataFrame(), "raw": {}}

    target = matched_hosts[0]
    relevant = audit_entries(rpc, expected_type)

    df = pd.DataFrame(relevant)
    label = target.get("name") or host_filter
    if df.empty:
        summary = f"No configuration changes are recorded for '{label}'."
    else:
        df['clock'] = pd.to_numeric(df['clock'], errors='coerce')
        df['changed_at'] = pd.to_datetime(df['clock'], unit='s')
        df = df.sort_values('clock', ascending=False)

        capped = " (at least — the query caps at 500)" if len(df) >= 500 else ""
        summary = f"Configuration changes recorded for '{label}': {len(df)}{capped}. "

        # Most of the volume is Zabbix updating template items on its own. "When
        # was this device's config changed" nearly always means the host record
        # or a change a person made, so lead with those.
        host_records = df[df['resource'] == 'host']
        if not host_records.empty:
            hr_row = host_records.iloc[-1]
            summary += (f"The host itself was {hr_row['change']} on "
                        f"{hr_row['changed_at']} UTC by {hr_row.get('username', 'unknown')}. ")

        by_person = df[df['username'].astype(str).str.lower() != 'system']
        if not by_person.empty:
            latest_person = by_person.iloc[0]
            summary += (f"{len(by_person)} change(s) were made by a person; most recent was "
                        f"{latest_person['change']} {latest_person['resource']} "
                        f"'{latest_person.get('resourcename')}' at "
                        f"{latest_person['changed_at']} UTC by {latest_person.get('username')}. ")
        else:
            summary += ("All recorded changes were automatic (made by System, "
                        "e.g. template updates). ")

        latest = df.iloc[0]
        summary += (f"Most recent change of any kind: {latest['change']} "
                    f"{latest['resource']} '{latest.get('resourcename')}' at "
                    f"{latest['changed_at']} UTC by {latest.get('username', '?')}. ")
        summary += "Breakdown by type: " + ", ".join(
            f"{kind}: {count}" for kind, count in df['resource'].value_counts().items()) + "."

    return {"summary": summary, "dataframe": df,
            "raw": {"audit_logs": df.to_dict('records') if not df.empty else [],
                    "host": target}}
