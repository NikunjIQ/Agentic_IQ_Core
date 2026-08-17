"""What a device is measuring: item readings for one host or a group of them.

Questions arrive scoped either way — "the uptime of 192.168.1.73" and "the
uptime of the routers" are the same question over a different set of hosts — so
resolution happens once and the readings are reported per host.
"""

import pandas as pd

from .metrics import render_metric, requested_metrics

# Upper bound on hosts read in one call. Every host multiplies the item search,
# and a question spanning more than this is really asking for a summary the
# hosts branch already gives.
MAX_HOSTS = 25


def _resolve_hosts(rpc, host_filter, group_filter):
    """Return (hosts, scope_label, error).

    A group name is tried whenever a host lookup comes up empty, because people
    ask for "the uptime of switches" exactly as often as they name a device, and
    'switches' is a group here rather than a host.
    """
    if group_filter:
        groups = rpc("hostgroup.get", {"output": ["groupid", "name"],
                                       "search": {"name": group_filter}})
        if not groups:
            return [], "", f"No host group matching '{group_filter}'."
        hosts = rpc("host.get", {"output": ["hostid", "name"],
                                 "groupids": [g["groupid"] for g in groups]})
        label = "group " + ", ".join(f"'{g['name']}'" for g in groups)
        if not hosts:
            return [], label, f"No hosts belong to {label}."
        return hosts, label, None

    hosts = rpc("host.get", {"output": ["hostid", "name"],
                             "search": {"name": host_filter, "host": host_filter},
                             "searchByAny": True})
    if hosts:
        return hosts, f"'{host_filter}'", None

    # Nothing matched as a host — the word may name a group instead.
    groups = rpc("hostgroup.get", {"output": ["groupid", "name"],
                                   "search": {"name": host_filter}})
    if groups:
        hosts = rpc("host.get", {"output": ["hostid", "name"],
                                 "groupids": [g["groupid"] for g in groups]})
        label = "group " + ", ".join(f"'{g['name']}'" for g in groups)
        if hosts:
            return hosts, label, None
        return [], label, f"No hosts belong to {label}."

    return [], "", (f"Could not find a host or host group matching "
                    f"'{host_filter}'.")


def query_items(rpc, host_filter=None, item_keyword=None, group_filter=None) -> dict:
    if not host_filter and not group_filter:
        return {"summary": "Please specify a host_filter (e.g. 192.168.1.73) or a "
                           "group_filter (e.g. 'Routers') to query items.",
                "dataframe": pd.DataFrame(), "raw": {}}

    hosts, scope, error = _resolve_hosts(rpc, host_filter, group_filter)
    if error:
        return {"summary": error, "dataframe": pd.DataFrame(), "raw": {}}

    truncated = 0
    if len(hosts) > MAX_HOSTS:
        truncated = len(hosts) - MAX_HOSTS
        hosts = hosts[:MAX_HOSTS]
    host_names = {str(h["hostid"]): h.get("name") or h.get("host", "") for h in hosts}

    # "cpu & memory utilization" asks for two metrics. Matching only the first
    # keyword silently answers half the question, so every metric mentioned is
    # collected and reported separately.
    metrics = requested_metrics(item_keyword)

    # 'state' separates "the check failed" from "the value is zero", and both
    # differ from "no such item" — three findings that otherwise all surface as
    # a blank cell. 'hostid' is what lets readings be reported per device.
    item_output = ["name", "key_", "lastvalue", "units", "state", "hostid"]
    searches = [k for m in metrics for k in m.get("key_searches", [])]
    hostids = list(host_names)

    items = rpc("item.get", {
        "output": item_output,
        "hostids": hostids,
        "search": {"key_": searches} if searches else {},
        "searchByAny": True,
    }) if searches else rpc("item.get", {
        "output": item_output,
        "hostids": hostids,
        "search": {"name": [item_keyword]} if item_keyword else {},
        "searchByAny": True,
    })

    by_host = {hid: [] for hid in host_names}
    for item in items:
        by_host.setdefault(str(item.get("hostid")), []).append(item)

    # Notices that describe the question rather than a device — EOL/EOS is the
    # same answer for every host, so it is stated once rather than per row.
    notices = [f"{m['label']}: {m['unavailable']}"
               for m in metrics if m.get("unavailable")]
    live_metrics = [m for m in metrics if not m.get("unavailable")]

    kept = []
    sections = []
    for hid, name in host_names.items():
        bits = []
        for metric in live_metrics or [{"label": item_keyword or "items",
                                        "kind": "numeric",
                                        "match": lambda i: True}]:
            # Interface counters repeat per port; the busiest few are what a
            # "what is the traffic on X" question is actually asking for.
            matched = [i for i in by_host.get(hid, []) if metric["match"](i)]
            text, rows = render_metric(metric, matched)
            bits.append(text)
            kept.extend({**r, "host": name} for r in rows)
        sections.append((name, bits))

    # Return only the items that answer the question. Handing back the raw search
    # result instead makes the preview table disagree with the summary — a
    # traffic question showed a table full of discard counters.
    df = pd.DataFrame(kept)

    if len(sections) == 1:
        name, bits = sections[0]
        summary = f"Host {name} — metrics for '{item_keyword}'. " + " || ".join(bits + notices)
    else:
        summary = (f"Metrics for '{item_keyword}' across {len(sections)} host(s) "
                   f"in {scope}. ")
        summary += " || ".join(f"{name}: {' '.join(bits)}" for name, bits in sections)
        if notices:
            summary += " || " + " || ".join(notices)
        if truncated:
            summary += (f" NOTE: {truncated} further host(s) in {scope} were not read "
                        f"— this covers the first {MAX_HOSTS}.")

    return {"summary": summary, "dataframe": df,
            "raw": {"items": kept, "hosts": list(host_names.values())}}
