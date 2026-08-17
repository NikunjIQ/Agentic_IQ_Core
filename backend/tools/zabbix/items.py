"""What a device is measuring: item readings for one host."""

import pandas as pd

from .metrics import render_metric, requested_metrics


def query_items(rpc, host_filter=None, item_keyword=None) -> dict:
    if not host_filter:
        return {"summary": "Please specify a host_filter to query items (e.g. 192.168.1.73).",
                "dataframe": pd.DataFrame(), "raw": {}}

    hosts = rpc("host.get", {"output": ["hostid", "name"],
                             "search": {"name": host_filter, "host": host_filter},
                             "searchByAny": True})
    if not hosts:
        return {"summary": f"Could not find host matching '{host_filter}'.",
                "dataframe": pd.DataFrame(), "raw": {}}

    hostid = hosts[0]["hostid"]
    host_label = hosts[0]["name"]

    # "cpu & memory utilization" asks for two metrics. Matching only the first
    # keyword silently answers half the question, so every metric mentioned is
    # collected and reported separately.
    metrics = requested_metrics(item_keyword)

    # 'state' separates "the check failed" from "the value is zero", and both
    # differ from "no such item" — three findings that otherwise all surface as
    # a blank cell.
    item_output = ["name", "key_", "lastvalue", "units", "state"]
    searches = [k for m in metrics for k in m.get("key_searches", [])]

    items = rpc("item.get", {
        "output": item_output,
        "hostids": hostid,
        "search": {"key_": searches} if searches else {},
        "searchByAny": True,
    }) if searches else rpc("item.get", {
        "output": item_output,
        "hostids": hostid,
        "search": {"name": [item_keyword]} if item_keyword else {},
        "searchByAny": True,
    })

    summary_bits = []
    kept = []
    for metric in metrics or [{"label": item_keyword or "items",
                               "kind": "numeric",
                               "match": lambda i: True}]:
        if metric.get("unavailable"):
            summary_bits.append(f"{metric['label']}: {metric['unavailable']}")
            continue
        # Interface counters repeat per port; the busiest few are what a "what is
        # the traffic on X" question is actually asking for.
        matched = [i for i in items if metric["match"](i)]
        text, rows = render_metric(metric, matched)
        summary_bits.append(text)
        kept.extend(rows)

    # Return only the items that answer the question. Handing back the raw search
    # result instead makes the preview table disagree with the summary — a
    # traffic question showed a table full of discard counters.
    df = pd.DataFrame(kept)
    # The per-metric notes carry the answer even when nothing was collected — an
    # unresponsive agent and a metric Zabbix does not track are both findings,
    # and both leave the table empty.
    if summary_bits:
        summary = (f"Host {host_label} — metrics for '{item_keyword}'. "
                   + " || ".join(summary_bits))
    else:
        summary = f"No items found on host {host_label} matching '{item_keyword}'."

    return {"summary": summary, "dataframe": df, "raw": {"items": kept}}
