"""The Zabbix tool: one LangChain tool, dispatching to tools/zabbix/.

The description below is what the model reads when deciding whether and how to
call this tool, so it is part of the prompt rather than documentation. Keep the
routing notes accurate: three separate bugs on this project came from the model
copying an example in a tool description as a literal argument value.
"""

from typing import Optional

import pandas as pd
from langchain_core.tools import tool

from tools.zabbix.audit import query_audit
from tools.zabbix.client import build_rpc
from tools.zabbix.hosts import query_hostgroups, query_hosts
from tools.zabbix.items import query_items
from tools.zabbix.metrics import requested_metrics
from tools.zabbix.monitoring import query_alerts, query_problems, query_triggers
from tools.zabbix.sla import query_sla

# Kept importable from here because the eval suite exercises the keyword routing
# directly — it is the cheapest check in the suite and costs no API call.
__all__ = ["query_zabbix", "requested_metrics"]


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
    - query_type: The type of Zabbix object to query (e.g., 'hosts', 'hostgroups', 'items', 'problems', 'alerts', 'triggers', 'audit', 'sla'). Default is 'hosts'. IMPORTANT: Use 'hosts' if you want to find total routers, total switches, or count specific types of devices! Use 'hostgroups' ONLY if the user specifically asks about host groups (e.g. 'Which host group has the most down devices?').
    - host_filter: Optional. Filters by partial name or hostname (e.g., 'web', 'db', '192.168.1.73'). Works for query_type='hosts', 'items', 'audit', 'sla' AND 'problems' — use it to ask what is wrong with one specific device.
    - status_filter: Optional. For query_type='hosts', filters by availability. Accepts several at once, e.g. 'up', 'down', 'unreachable or unknown'. Note 'down' includes hosts that are unreachable (not responding). For query_type='problems', pass a minimum severity digit instead (0=Not classified, 1=Information, 2=Warning, 3=Average, 4=High, 5=Disaster).
    - group_filter: Optional. Filters hosts by host group name (e.g., 'Routers', 'Switches', 'Servers'). Only applies when query_type='hosts'. Counts of routers and switches are also included in the 'hosts' summary automatically.
    - item_keyword: Optional. Metric(s) to read, with query_type='items'. Name EVERY metric asked for in one call — 'cpu & memory' returns both. Recognised: cpu, memory, traffic/network, disk, uptime, OS/IOS/software version, agent version, hardware model/serial, ping/latency/loss.
    - date_filter: Optional. Filters by relative date (e.g., 'today'). Applies to query_type='alerts'.

    ROUTING NOTES:
    - "how many alerts are active / what is wrong right now" -> use query_type='problems', NOT 'alerts'. Alerts are records of notifications that were sent; problems are live conditions.
    - "how many alerts were sent today" -> query_type='alerts' with date_filter='today'.
    - "what is the problem with <device>" -> query_type='problems' with host_filter=<device>.
    - "when was the config of <device> changed" -> query_type='audit' with host_filter=<device>.
    - "what IOS / software version / uptime / model / serial does <device> run" -> query_type='items' with host_filter=<device> and the matching item_keyword.
    - "how many devices run Zabbix Agent 2 / which agent versions are deployed" -> query_type='hosts'. The estate-wide agent version breakdown is in the hosts summary. Agent 1 vs Agent 2 cannot be distinguished — say so; do not infer it from the version number. For one device use query_type='items' with item_keyword='agent version'.
    - "what is the SLA / service level / uptime target for <device or the estate>" -> query_type='sla', optionally with host_filter. Returns each SLA's target, the achieved SLI per service, downtime, and which services no SLA covers at all. Do NOT use query_type='items' with item_keyword='uptime' for this — that is raw device uptime, not a service level.
    - "what is the EOL/EOS of <device>" -> query_type='items' with item_keyword='eol'. Zabbix holds no lifecycle data; the tool says so and returns the model and serial an external lookup needs.
    """
    result = {"summary": "", "dataframe": pd.DataFrame(), "raw": {}}

    try:
        rpc = build_rpc()
        q_type = (query_type or "hosts").lower()

        if q_type == "hosts":
            result = query_hosts(rpc, host_filter=host_filter,
                                 status_filter=status_filter, group_filter=group_filter)
        elif q_type == "hostgroups":
            result = query_hostgroups(rpc)
        elif q_type == "items":
            result = query_items(rpc, host_filter=host_filter, item_keyword=item_keyword)
        elif q_type == "problems":
            result = query_problems(rpc, host_filter=host_filter,
                                    status_filter=status_filter)
        elif q_type == "alerts":
            result = query_alerts(rpc, date_filter=date_filter)
        elif q_type == "triggers":
            result = query_triggers(rpc)
        elif q_type == "audit":
            result = query_audit(rpc, host_filter=host_filter)
        elif q_type in ("sla", "slas", "sli", "services", "service"):
            result = query_sla(rpc, host_filter=host_filter)
        else:
            result = {"summary": f"Unsupported query_type: {query_type}",
                      "dataframe": pd.DataFrame(), "raw": {}}

    except Exception as e:
        # A rate limit or an outage should read as a sentence, not a 500.
        result = {"summary": f"Zabbix API Error: {str(e)}",
                  "dataframe": pd.DataFrame(), "raw": {}}

    df = result.get("dataframe")
    if df is None:
        df = pd.DataFrame()
    if not df.empty:
        # Streamlit and JSON serialisation both choke on nested cells.
        for col in df.columns:
            if df[col].apply(lambda x: isinstance(x, (list, dict))).any():
                df[col] = df[col].astype(str)

    return {"summary": result.get("summary", ""), "dataframe": df,
            "raw": result.get("raw", {})}
