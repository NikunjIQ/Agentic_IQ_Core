from tools.zammad_tool import query_zammad
from tools.zabbix_tool import query_zabbix
from tools.prometheus_tool import query_prometheus
from tools.opensearch_tool import query_opensearch
from tools.ceph_tool import query_ceph
from tools.correlation import correlate_problems_and_tickets, lookup_ticket_context

LANGCHAIN_TOOLS = [
    query_zammad,
    query_zabbix,
    query_prometheus,
    query_opensearch,
    query_ceph,
    correlate_problems_and_tickets,
    lookup_ticket_context,
]

# One line per tool, for the classifier only.
#
# Classification answers "which system owns this question?" and returns a tool
# NAME — it never emits arguments. Feeding it the full docstrings cost 2,829
# tokens per question of parameter documentation and routing notes it cannot
# act on, which on an 8,000 TPM budget was 40% of a minute spent before any
# work began.
#
# What must survive is the domain boundary. The four systems watch different,
# non-overlapping machines, and blurring that is this app's most common way to
# produce a confidently wrong answer — so each line names the estate, not the
# capability.
ROUTING_SUMMARIES = {
    "query_zabbix": (
        "Zabbix monitoring. Cisco routers and switches (192.168.1.73/.74/.75), "
        "Windows hosts, a few Linux boxes. Host counts and up/down status, host "
        "groups, live problems, alerts, triggers, device metrics (CPU, memory, "
        "traffic, disk, uptime, OS/IOS version, model, serial), config audit, "
        "SLAs and service levels."
    ),
    "query_zammad": (
        "Zammad helpdesk. Tickets only — counts, states, priorities, agents, "
        "organisations, SLA and response times, tags, free-text search, named "
        "queues, booked time, one ticket's conversation, group_by breakdowns."
    ),
    "query_prometheus": (
        "Prometheus. The Linux cluster server01/02/03, OpenStack, VMs and "
        "containers — a different estate from Zabbix. CPU, memory, disk, load, "
        "uptime, network, scrape targets, OpenStack routers/networks, and Cinder "
        "volume/snapshot COUNTS from the OpenStack exporter — the RBD snapshots "
        "themselves are Ceph. Raw PromQL."
    ),
    "query_opensearch": (
        "OpenSearch. OpenStack application logs only (flog-*). Holds nothing "
        "about network devices, so a zero result means 'not collected here', "
        "never 'nothing is wrong'."
    ),
    "correlate_problems_and_tickets": (
        "Joins Zabbix problems to their Zammad tickets on an exact tag. Use for "
        "'is anyone working on this', or to find problems with no ticket raised."
    ),
    "query_ceph": (
        "Ceph storage cluster. The disks UNDER OpenStack: capacity and free space, "
        "pools (Cinder volumes, Glance images, Nova VM disks, backups), OSDs and "
        "disk failures, RBD images and their SNAPSHOTS, CephFS and MDS, CRUSH rules "
        "and replication, cluster hosts, cluster logs, health and storage alerts, "
        "object storage (RGW, S3 buckets), and the monitoring endpoints its own "
        "dashboard records — asking for the Grafana or Prometheus URL is a Ceph "
        "question, not a Prometheus one. Prometheus watches VM CPU and memory; "
        "Ceph watches the storage beneath them. Not a source of VM counts."
    ),
    "lookup_ticket_context": (
        "The reverse join: from a Zammad ticket id back to the device and the "
        "Zabbix problem that raised it."
    ),
}

TOOL_REGISTRY = {
    tool_obj.name: {
        "function": tool_obj,
        "description": tool_obj.description,
        # Falls back to the full description so a newly registered tool is
        # routable before anyone writes its summary.
        "routing_summary": ROUTING_SUMMARIES.get(tool_obj.name, tool_obj.description),
        "tool_object": tool_obj
    }
    for tool_obj in LANGCHAIN_TOOLS
}

