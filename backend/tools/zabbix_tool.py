import os
import requests
import pandas as pd
from typing import Optional
from langchain_core.tools import tool

from tools.clients import SEVERITY_LABELS, TICKET_ID_TAG

# Zabbix 6.0+ removed the host-level "available" field. Passive availability now
# lives on each interface, and active-agent availability on the host's
# "active_available". Every branch that needs availability must request the same
# fields and derive status the same way, so both live here.
_HOST_AVAILABILITY_PARAMS = {
    "output": ["hostid", "host", "name", "status", "active_available"],
    "selectInterfaces": ["type", "details", "available", "error"],
    "selectHostGroups": ["name", "groupid"]
}

# A host counts as "down" for reporting purposes if it is not answering, whether or
# not Zabbix attached a reason. An interface that goes unavailable almost always
# carries an error string, so counting only 'down' would report zero forever.
_OFFLINE_STATUSES = ('down', 'unreachable')

# What a user means by each status word. "Down" has to cover 'unreachable' or the
# tool contradicts itself: the host-group counts treat a non-responding host as
# down, so asking "how many devices are down" must not answer 0 while asking
# "which group has the most down devices" answers 2.
_STATUS_ALIASES = {
    'unreachable': ('unreachable',),
    'unknown': ('unknown',),
    'offline': _OFFLINE_STATUSES,
    'down': _OFFLINE_STATUSES,
    'up': ('up',),
}


def _parse_status_filter(status_filter: str) -> set:
    """Turn 'down', 'unreachable or unknown', 'up,down' into a set of statuses.

    Each status word is matched independently so multi-status questions
    ("list hosts that are unreachable or unknown") do not silently return only
    the first one that matched.
    """
    text = (status_filter or '').lower()
    wanted = set()
    for keyword, statuses in _STATUS_ALIASES.items():
        if keyword in text:
            wanted.update(statuses)
    return wanted


# auditlog.get resource types and actions, for turning numeric codes into words.
_AUDIT_RESOURCE_TYPES = {
    4: "host", 6: "graph", 13: "trigger", 15: "item",
    16: "image", 17: "value map", 19: "IT service", 30: "template",
}
_AUDIT_ACTIONS = {
    0: "added", 1: "updated", 2: "deleted", 3: "logged in", 4: "logged out",
    5: "executed", 6: "failed login", 7: "history cleared",
    8: "enabled", 9: "disabled",
}


def _numeric(value):
    """Sort key for item values; non-numeric readings sink to the bottom."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return float('-inf')


def _requested_metrics(item_keyword: str) -> list:
    """Every metric named in the keyword, not just the first one recognised.

    'cpu & memory utilization' has to come back with both. Each entry also carries
    a precise match test, because Zabbix key searches are substring-based: asking
    for 'net.if' also returns discard and error counters, which is why a plain
    traffic question used to answer with "Inbound packets discarded: 0".
    """
    text = (item_keyword or '').lower()
    metrics = []

    if 'cpu' in text or 'processor' in text:
        metrics.append({
            'label': 'CPU',
            'key_search': 'cpu',
            'match': lambda i: 'cpu' in (i.get('key_', '') + i.get('name', '')).lower(),
        })
    if 'memory' in text or 'ram' in text:
        metrics.append({
            'label': 'Memory',
            'key_search': 'memory',
            'match': lambda i: 'memory' in (i.get('key_', '') + i.get('name', '')).lower(),
        })
    if any(w in text for w in ('traffic', 'network', 'bandwidth', 'throughput')):
        metrics.append({
            'label': 'Network traffic (bits in/out)',
            'key_search': 'net.if',
            # net.if.in.discards / .errors / .speed / .status also match "net.if",
            # so pin to the exact keys that carry actual throughput.
            'match': lambda i: i.get('key_', '').split('[')[0] in ('net.if.in', 'net.if.out'),
        })
    if any(w in text for w in ('disk', 'space', 'filesystem', 'storage')):
        metrics.append({
            'label': 'Disk space',
            'key_search': 'vfs.fs',
            'match': lambda i: i.get('key_', '').startswith('vfs.fs'),
        })
    return metrics


def _derive_availability(host: dict) -> str:
    """Map a Zabbix 6.0+ host object to 'up', 'down', 'unreachable' or 'unknown'.

    Expects the host to have been fetched with _HOST_AVAILABILITY_PARAMS.
    """
    interfaces = host.get('interfaces', [])
    avail_set = set(str(i.get('available', '0')) for i in interfaces)
    active_avail = str(host.get('active_available', '0'))

    if '2' in avail_set or active_avail == '2':
        status = 'down'
    elif '1' in avail_set or active_avail == '1':
        status = 'up'
    else:
        status = 'unknown'

    # An interface that failed carries the reason it failed; surface that as
    # 'unreachable' so callers can tell "no response" from "reported down".
    if any(i.get('error') for i in interfaces):
        status = 'unreachable'

    return status


@tool
def query_zabbix(
    question: Optional[str] = None, 
    query_type: Optional[str] = 'hosts', 
    host_filter: Optional[str] = None, 
    status_filter: Optional[str] = None,
    group_filter: Optional[str] = None,
    item_keyword: Optional[str] = None,
    date_filter: Optional[str] = None
) -> dict:
    """
    Query the Zabbix infrastructure monitoring system.
    Use this tool to inspect active host alerts, network device problems, system triggers,
    ICMP ping availability, disk space alerts, host counts, and host group status.
    - query_type: The type of Zabbix object to query (e.g., 'hosts', 'hostgroups', 'items', 'problems', 'alerts', 'triggers', 'audit'). Default is 'hosts'. IMPORTANT: Use 'hosts' if you want to find total routers, total switches, or count specific types of devices! Use 'hostgroups' ONLY if the user specifically asks about host groups (e.g. 'Which host group has the most down devices?').
    - host_filter: Optional. Filters by partial name or hostname (e.g., 'web', 'db', '192.168.1.73'). Works for query_type='hosts', 'items', 'audit' AND 'problems' — use it to ask what is wrong with one specific device.
    - status_filter: Optional. For query_type='hosts', filters by availability. Accepts several at once, e.g. 'up', 'down', 'unreachable or unknown'. Note 'down' includes hosts that are unreachable (not responding). For query_type='problems', pass a minimum severity digit instead (0=Not classified, 1=Information, 2=Warning, 3=Average, 4=High, 5=Disaster).
    - group_filter: Optional. Filters hosts by host group name (e.g., 'Routers', 'Switches', 'Servers'). Only applies when query_type='hosts'. Counts of routers and switches are also included in the 'hosts' summary automatically.
    - item_keyword: Optional. Metric(s) to read. Name EVERY metric asked for in one call — 'cpu & memory' returns both, 'traffic' returns interface throughput, 'disk' returns filesystem space.
    - date_filter: Optional. Filters by relative date (e.g., 'today'). Applies to query_type='alerts'.

    ROUTING NOTES:
    - "how many alerts are active / what is wrong right now" -> use query_type='problems', NOT 'alerts'. Alerts are records of notifications that were sent; problems are live conditions.
    - "how many alerts were sent today" -> query_type='alerts' with date_filter='today'.
    - "what is the problem with <device>" -> query_type='problems' with host_filter=<device>.
    - "when was the config of <device> changed" -> query_type='audit' with host_filter=<device>.
    - Zabbix reports whether a host is monitored via SNMP or via a Zabbix agent, but it does NOT distinguish Zabbix Agent 1 from Agent 2. Report the agent-monitored count and say the version split is not available.
    """

    zabbix_url = os.getenv("ZABBIX_URL", "http://192.168.1.205:8080")
    if not zabbix_url.endswith("api_jsonrpc.php"):
        zabbix_url = zabbix_url.rstrip("/") + "/api_jsonrpc.php"

    zabbix_token = os.getenv("ZABBIX_API_TOKEN", "")
    zabbix_user = os.getenv("ZABBIX_USER", "")
    zabbix_pass = os.getenv("ZABBIX_PASS", "")

    headers = {"Content-Type": "application/json-rpc"}

    if zabbix_token:
        headers["Authorization"] = f"Bearer {zabbix_token}"
        auth_param = None
    elif zabbix_user and zabbix_pass:
        login_payload = {
            "jsonrpc": "2.0",
            "method": "user.login",
            "params": {"user": zabbix_user, "password": zabbix_pass},
            "id": 1
        }
        res = requests.post(zabbix_url, headers=headers, json=login_payload, timeout=10)
        res.raise_for_status()
        auth_param = res.json().get("result")
    else:
        auth_param = None

    def rpc(method, params):
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": 2
        }
        if auth_param:
            payload["auth"] = auth_param
        r = requests.post(zabbix_url, headers=headers, json=payload, timeout=30)
        data = r.json()
        if "error" in data:
            raise Exception(data["error"])
        return data.get("result", [])

    summary = ""
    df = pd.DataFrame()
    raw = {}

    try:
        q_type = (query_type or "hosts").lower()
        
        if q_type == "hosts":
            hosts = rpc("host.get", _HOST_AVAILABILITY_PARAMS)

            for h in hosts:
                h['available_str'] = _derive_availability(h)

                types = [str(i.get('type')) for i in h.get('interfaces', [])]
                h['is_zabbix_agent'] = '1' in types
                h['is_snmp'] = '2' in types
                
                groups = [g.get('name', '').lower() for g in h.get('hostgroups', [])]
                h['is_router'] = any('router' in g for g in groups)
                h['is_switch'] = any('switch' in g for g in groups)
                h['group_names'] = ", ".join(g.get('name', '') for g in h.get('hostgroups', []))

            df = pd.DataFrame(hosts)
            if not df.empty:
                if host_filter:
                    df = df[df['host'].str.contains(host_filter, case=False, na=False) | df['name'].str.contains(host_filter, case=False, na=False)]
                if group_filter:
                    g_lower = group_filter.lower()
                    if 'router' in g_lower:
                        df = df[df['is_router'] == True]
                    elif 'switch' in g_lower:
                        df = df[df['is_switch'] == True]
                    else:
                        df = df[df['group_names'].str.contains(g_lower, case=False, na=False)]
                if status_filter:
                    wanted = _parse_status_filter(status_filter)
                    if wanted:
                        df = df[df['available_str'].isin(wanted)]

                total = len(df)
                up = int((df['available_str'] == 'up').sum())
                unknown = int((df['available_str'] == 'unknown').sum())
                offline_mask = df['available_str'].isin(_OFFLINE_STATUSES)
                offline = int(offline_mask.sum())
                snmp = int(df['is_snmp'].sum()) if 'is_snmp' in df.columns else 0
                agent = int(df['is_zabbix_agent'].sum()) if 'is_zabbix_agent' in df.columns else 0

                summary = (
                    f"Total hosts matching criteria: {total}. "
                    f"Up: {up}. Down (not responding): {offline}. Unknown/not yet checked: {unknown}. "
                    f"SNMP-monitored devices: {snmp}. Zabbix-agent-monitored devices: {agent}."
                )
                if offline:
                    offline_hosts = df[offline_mask]['name'].tolist()
                    summary += f" Down hosts: {', '.join(offline_hosts)}."
                if 'is_router' in df.columns:
                    summary += (f" Of these, {int(df['is_router'].sum())} are routers and "
                                f"{int(df['is_switch'].sum())} are switches.")
                if total <= 25:
                    listing = ", ".join(f"{r['name']} ({r['available_str']})" for _, r in df.iterrows())
                    summary += f" Hosts: {listing}."
            else:
                summary = "No hosts found matching criteria."
            
            raw = {"hosts": df.to_dict('records') if not df.empty else []}

        elif q_type == "hostgroups":
            groups = rpc("hostgroup.get", {"output": ["groupid", "name"]})
            hosts = rpc("host.get", _HOST_AVAILABILITY_PARAMS)

            group_stats = {g['groupid']: {'total': 0, 'down_names': []} for g in groups}
            for h in hosts:
                is_offline = _derive_availability(h) in _OFFLINE_STATUSES
                host_name = h.get('name') or h.get('host', '')
                for g in h.get('hostgroups', []):
                    stats = group_stats.get(g.get('groupid'))
                    if stats is None:
                        continue
                    stats['total'] += 1
                    if is_offline:
                        stats['down_names'].append(host_name)

            for g in groups:
                stats = group_stats[g['groupid']]
                g['total_devices'] = stats['total']
                g['down_devices'] = len(stats['down_names'])
                g['down_device_names'] = ", ".join(stats['down_names'])

            df = pd.DataFrame(groups)
            total_down = sum(len(s['down_names']) for s in group_stats.values())
            summary = f"Total host groups configured: {len(groups)}."
            if not df.empty:
                df = df.sort_values('down_devices', ascending=False)
                highest = df.iloc[0]
                if highest['down_devices'] > 0:
                    summary += (
                        f" Group '{highest['name']}' has the highest number of down devices "
                        f"({highest['down_devices']} of {highest['total_devices']} devices in the group)."
                        f" Down devices in that group: {highest['down_device_names']}."
                    )
                else:
                    summary += " No host group currently has any down devices."
            raw = {"groups": groups, "total_down_devices": total_down}
            
        elif q_type == "items":
            if not host_filter:
                return {"summary": "Please specify a host_filter to query items (e.g. 192.168.1.73).", "dataframe": pd.DataFrame(), "raw": {}}
                
            hosts = rpc("host.get", {"output": ["hostid", "name"], "search": {"name": host_filter, "host": host_filter}, "searchByAny": True})
            if not hosts:
                return {"summary": f"Could not find host matching '{host_filter}'.", "dataframe": pd.DataFrame(), "raw": {}}
                
            hostid = hosts[0]["hostid"]
            host_label = hosts[0]["name"]

            # "cpu & memory utilization" asks for two metrics. Matching only the
            # first keyword silently answers half the question, so every metric
            # mentioned is collected and reported separately.
            metrics = _requested_metrics(item_keyword)

            items = rpc("item.get", {
                "output": ["name", "key_", "lastvalue", "units"],
                "hostids": hostid,
                "search": {"key_": [m["key_search"] for m in metrics]} if metrics else {},
                "searchByAny": True,
            }) if metrics else rpc("item.get", {
                "output": ["name", "key_", "lastvalue", "units"],
                "hostids": hostid,
                "search": {"name": [item_keyword]} if item_keyword else {},
                "searchByAny": True,
            })

            summary_bits = []
            kept = []
            for metric in metrics or [{"label": item_keyword or "items", "match": lambda i: True}]:
                matched = [i for i in items if metric["match"](i)]
                if not matched:
                    summary_bits.append(f"{metric['label']}: no items found.")
                    continue
                # Interface counters repeat per port; the busiest few are what
                # a "what is the traffic on X" question is actually asking for.
                matched.sort(key=lambda i: _numeric(i.get("lastvalue")), reverse=True)
                for i in matched:
                    kept.append({**i, "metric": metric["label"]})
                shown = [f"{i['name']}: {i.get('lastvalue')} {i.get('units', '')}".strip()
                         for i in matched[:8]]
                summary_bits.append(
                    f"{metric['label']} ({len(matched)} item(s), highest first): " + "; ".join(shown))

            # Return only the items that answer the question. Handing back the raw
            # search result instead makes the preview table disagree with the
            # summary — a traffic question showed a table full of discard counters.
            df = pd.DataFrame(kept)
            if df.empty:
                summary = f"No items found on host {host_label} matching '{item_keyword}'."
            else:
                summary = (f"Host {host_label} — metrics for '{item_keyword}'. "
                           + " || ".join(summary_bits))
            raw = {"items": kept}
            
        elif q_type == "problems":
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
                p["severity_label"] = SEVERITY_LABELS.get(int(p.get("severity", 0) or 0), p.get("severity"))
                tags = {t.get("tag"): t.get("value") for t in (p.get("tags") or [])}
                # the Zabbix->Zammad webhook writes the created ticket id back here
                p["zammad_ticket_id"] = tags.get(TICKET_ID_TAG)
                p["has_ticket"] = bool(p["zammad_ticket_id"])
                p["tags"] = str(p.get("tags"))

            df = pd.DataFrame(problems)
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
                               f"{with_ticket} already have a Zammad ticket, {len(df) - with_ticket} do not. ")
                    details = [f"{r['host']}: {r['name']} ({r['severity_label']})"
                               for _, r in df.head(8).iterrows()]
                    summary += "Problems: " + " | ".join(details)
            else:
                summary = "There are 0 active problems right now."
            raw = {"problems": df.to_dict("records") if not df.empty else []}
            
        elif q_type == "alerts":
            params = {"output": "extend", "sortfield": "clock", "sortorder": "DESC"}
            if date_filter and "today" in date_filter.lower():
                import time
                from datetime import date
                today_start = int(time.mktime(date.today().timetuple()))
                params["time_from"] = today_start
                
            alerts = rpc("alert.get", params)
            df = pd.DataFrame(alerts)
            # Scope and the problems/alerts caveat belong on the empty answer too —
            # "0 alerts today" is a different statement from "0 alerts ever".
            scope = "sent today" if date_filter and "today" in date_filter.lower() else "in total (all time)"
            if not df.empty:
                df['clock'] = pd.to_numeric(df['clock'], errors='coerce')
                df['clock_dt'] = pd.to_datetime(df['clock'], unit='s')

                summary = f"Alerts {scope}: {len(alerts)}. "

                # An alert is a notification Zabbix tried to deliver, not a live
                # condition. Without this breakdown "how many alerts are active"
                # gets answered with the all-time total.
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
            raw = {"alerts": alerts}
            
        elif q_type == "triggers":
            triggers = rpc("trigger.get", {"output": ["triggerid", "description", "status"]})
            df = pd.DataFrame(triggers)
            enabled = sum(1 for t in triggers if str(t.get('status')) == '0')
            disabled = sum(1 for t in triggers if str(t.get('status')) == '1')
            summary = f"Total triggers: {len(triggers)}. Enabled: {enabled}. Disabled: {disabled}."
            raw = {"triggers": triggers}
            
        elif q_type == "audit":
            if not host_filter:
                return {"summary": "Please specify a host_filter (device) to check config changes.", "dataframe": pd.DataFrame(), "raw": {}}

            # Resolve the device first. The audit log is dominated by automatic
            # item/trigger edits, so scanning "the last 100 entries" and grepping
            # for a hostname finds nothing — host changes are older than the
            # background churn and are keyed by hostid, not by name.
            matched_hosts = rpc("host.get", {
                "output": ["hostid", "host", "name"],
                "search": {"name": host_filter, "host": host_filter},
                "searchByAny": True,
            })
            if not matched_hosts:
                return {"summary": f"Could not find a host matching '{host_filter}'.",
                        "dataframe": pd.DataFrame(), "raw": {}}

            target = matched_hosts[0]
            host_ids = {str(h["hostid"]) for h in matched_hosts}

            # Ask the audit log for this device's records by id. Sorting the whole
            # log by time and taking the newest N never reaches them — background
            # item/trigger edits generate far more entries than host changes do.
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

            logs = rpc("auditlog.get", {
                "output": "extend",
                "filter": {"resourceid": sorted(expected_type)},
                "sortfield": "clock", "sortorder": "DESC",
                "limit": 500,
            })

            relevant = []
            for entry in logs:
                rid = str(entry.get("resourceid"))
                # ids are only unique within a resource type, so confirm the pair
                if expected_type.get(rid) != str(entry.get("resourcetype")):
                    continue
                entry["resource"] = _AUDIT_RESOURCE_TYPES.get(int(entry.get("resourcetype", -1)), "other")
                entry["change"] = _AUDIT_ACTIONS.get(int(entry.get("action", -1)), entry.get("action"))
                entry["details"] = str(entry.get("details"))[:200]
                relevant.append(entry)

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

                # Most of the volume is Zabbix updating template items on its own.
                # "When was this device's config changed" nearly always means the
                # host record or a change a person made, so lead with those.
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
                    summary += "All recorded changes were automatic (made by System, e.g. template updates). "

                latest = df.iloc[0]
                summary += (f"Most recent change of any kind: {latest['change']} "
                            f"{latest['resource']} '{latest.get('resourcename')}' at "
                            f"{latest['changed_at']} UTC by {latest.get('username', '?')}. ")
                summary += "Breakdown by type: " + ", ".join(
                    f"{kind}: {count}" for kind, count in df['resource'].value_counts().items()) + "."
            raw = {"audit_logs": df.to_dict('records') if not df.empty else [], "host": target}
            
        else:
            summary = f"Unsupported query_type: {query_type}"
            
    except Exception as e:
        summary = f"Zabbix API Error: {str(e)}"
        
    if not df.empty:
        for col in df.columns:
            if df[col].apply(lambda x: isinstance(x, (list, dict))).any():
                df[col] = df[col].astype(str)
                
    return {"summary": summary, "dataframe": df, "raw": raw}
