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
    query_type: Optional[str] = 'hosts',
    host_filter: Optional[str] = None,
    status_filter: Optional[str] = None,
    group_filter: Optional[str] = None,
    item_keyword: Optional[str] = None,
    date_filter: Optional[str] = None
) -> dict:
    """
    Query Zabbix: Cisco routers/switches, Windows hosts, a few Linux boxes.

    - query_type: 'hosts' (default) | 'hostgroups' | 'items' | 'problems' | 'alerts' | 'triggers' | 'audit' | 'sla'.
      'hosts' = counts and up/down status, including totals for routers and switches.
      'hostgroups' = only when the question is about the groups themselves.
      'items' = metric VALUES. 'problems' = what is wrong now. 'alerts' = notifications
      that were sent. 'audit' = config changes. 'sla' = service-level targets and achieved figures.
    - host_filter: one device by name or IP. Applies to hosts, items, problems, audit, sla.
    - group_filter: a host group ('Routers', 'Switches'). Applies to hosts and items.
    - status_filter: for 'hosts', availability — accepts several at once ('up',
      'unreachable or unknown'); 'down' includes unreachable. For 'problems', a minimum
      severity digit 0-5.
    - item_keyword: with 'items', the metric(s). Name EVERY metric asked for in one call.
      Recognised: cpu, memory, traffic, disk, uptime, OS/IOS/software version, agent
      version, hardware model/serial, ping/latency/loss, eol.
    - date_filter: with 'alerts', a relative date such as 'today'.

    ROUTING RULES:
    - "what is wrong / how many alerts are active" -> 'problems', never 'alerts'.
      Alerts are notification records; problems are live conditions.
    - A metric for a CLASS of devices is still 'items' — use group_filter, never 'hosts'.
      'hosts' reports status and counts and cannot report a metric value.
    - Service level or SLA -> 'sla', not item_keyword='uptime'. Device uptime and a
      service level are different figures.
    - Agent 1 vs Agent 2 cannot be distinguished; report versions and say so.
    - Zabbix holds no EOL/EOS data; item_keyword='eol' returns that statement plus the
      model and serial an external lookup needs.
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
            result = query_items(rpc, host_filter=host_filter, item_keyword=item_keyword,
                                 group_filter=group_filter)
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
