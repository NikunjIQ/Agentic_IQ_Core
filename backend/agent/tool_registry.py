from tools.zammad_tool import query_zammad
from tools.zabbix_tool import query_zabbix
from tools.prometheus_tool import query_prometheus
from tools.correlation import correlate_problems_and_tickets, lookup_ticket_context

LANGCHAIN_TOOLS = [
    query_zammad,
    query_zabbix,
    query_prometheus,
    correlate_problems_and_tickets,
    lookup_ticket_context,
]

TOOL_REGISTRY = {
    tool_obj.name: {
        "function": tool_obj,
        "description": tool_obj.description,
        "tool_object": tool_obj
    }
    for tool_obj in LANGCHAIN_TOOLS
}

