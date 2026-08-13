"""The questions this assistant is expected to answer, and how to call them.

This is the corpus the tool layer is graded against. Each entry pairs the
question a person would ask with the tool arguments that should answer it, so a
change in routing or argument handling shows up as a failing expectation rather
than a subtly different answer nobody notices.

Expectations are deliberately about SHAPE, not exact values: the live estate
changes hourly, so asserting "3 hosts are down" would fail by tomorrow while
telling us nothing about correctness.
"""

# --- Zabbix: network devices and Windows hosts ------------------------------
ZABBIX_QUERIES = [
    ("total hosts", {"query_type": "hosts"}, "Total hosts matching criteria"),
    ("total routers", {"query_type": "hosts", "group_filter": "Routers"}, "are routers"),
    ("total switches", {"query_type": "hosts", "group_filter": "Switches"}, "are switches"),
    ("devices up", {"query_type": "hosts", "status_filter": "up"}, "Up:"),
    ("devices down", {"query_type": "hosts", "status_filter": "down"}, "Down (not responding)"),
    ("host group with most down", {"query_type": "hostgroups"}, "Total host groups configured"),
    ("cpu of a host", {"query_type": "items", "host_filter": "192.168.1.73",
                       "item_keyword": "cpu"}, "CPU"),
    ("cpu AND memory of a host", {"query_type": "items", "host_filter": "192.168.1.73",
                                  "item_keyword": "cpu & memory"}, "Memory"),
    ("unreachable or unknown", {"query_type": "hosts",
                                "status_filter": "unreachable or unknown"}, "Total hosts"),
    ("alerts", {"query_type": "alerts"}, "Alerts"),
    ("status of one host", {"query_type": "hosts", "host_filter": "192.168.1.75"}, "Total hosts"),
    ("active problems", {"query_type": "problems"}, "problems"),
    ("network traffic", {"query_type": "items", "host_filter": "192.168.1.73",
                         "item_keyword": "traffic"}, "Network traffic"),
    ("alerts today", {"query_type": "alerts", "date_filter": "today"}, "Alerts sent today"),
    ("triggers enabled vs disabled", {"query_type": "triggers"}, "Total triggers"),
    ("problem with one device", {"query_type": "problems",
                                 "host_filter": "192.168.1.74"}, "problems"),
    ("config change of a device", {"query_type": "audit",
                                   "host_filter": "192.168.1.80"}, "Configuration changes"),
]

# --- Zammad: helpdesk -------------------------------------------------------
ZAMMAD_QUERIES = [
    ("opened last 10 days", {"date_filter": "last 10 days", "date_field": "created"},
     "FILTERED RESULT"),
    ("open state", {"state_filter": "open"}, "FILTERED RESULT"),
    ("work in progress", {"state_filter": "Work in Progress"}, "FILTERED RESULT"),
    ("closed last 10 days", {"date_filter": "last 10 days", "date_field": "closed"},
     "FILTERED RESULT"),
    ("escalated", {"state_filter": "Escalated"}, "FILTERED RESULT"),
    ("priority 2", {"priority_filter": "2"}, "FILTERED RESULT"),
    ("handled by an agent", {"owner_filter": "sourabh"}, "FILTERED RESULT"),
    ("created by a user", {"customer_filter": "sourabh14280@gmail.com"}, "FILTERED RESULT"),
    ("from an organization", {"organization_filter": "Identiqa"}, "FILTERED RESULT"),
    ("updated today", {"date_filter": "today", "date_field": "updated"}, "FILTERED RESULT"),
    ("agent who resolved most", {"state_filter": "closed", "group_by": "agent"},
     "BREAKDOWN BY AGENT"),
    ("by organization", {"group_by": "organization"}, "BREAKDOWN BY ORGANIZATION"),
    ("estate statistics", {}, "Average first response time"),
]

# Facts the unfiltered Zammad summary must always carry, because several
# expected questions are answered from it rather than from a filter.
ZAMMAD_SUMMARY_MUST_MENTION = [
    "By state:", "By priority:", "Unassigned tickets:", "Created today:",
    "Opened in last 10 days:", "No update in last 10 days:", "Closed in last 10 days:",
    "Average first response time:", "SLA", "Largest tags:", "per day over",
]

# --- Prometheus: Linux cluster, OpenStack, containers -----------------------
PROMETHEUS_QUERIES = [
    ("host cpu", {"metric": "cpu"}, "CPU used %"),
    ("host memory", {"metric": "memory"}, "Memory used %"),
    ("host disk", {"metric": "disk"}, "Disk used %"),
    ("scrape targets", {"metric": "targets"}, "scraping"),
    ("alerts", {"metric": "alerts"}, "alert"),
    ("metric discovery", {"search": "slow_queries"}, "metric name(s) match"),
    ("raw promql", {"promql": "sum by (job) (up)"}, "Custom query"),
    ("vm inventory", {"metric": "vms"}, "VM(s) known to OpenStack/libvirt"),
    ("vm utilisation", {"metric": "vm_utilization"}, "VM resource utilisation"),
    ("recently created vms", {"metric": "vm_new", "range_window": "7d"}, "appeared in the last"),
    ("neutron routers", {"metric": "routers"}, "Neutron"),
    ("network placement", {"metric": "networks"}, "private/internal"),
    ("volumes and snapshots", {"metric": "volumes"}, "Cinder storage"),
    ("containers", {"metric": "containers"}, "cAdvisor"),
]

# Limits the tools must state rather than paper over. Each entry is
# (tool arguments, phrase that must appear). These guard the honesty of the
# answers — the failure mode they prevent is confident fabrication.
HONESTY_EXPECTATIONS = [
    ({"metric": "vms"}, "INFERRED FROM NAMES ONLY"),
    ({"metric": "vm_utilization"}, "CANNOT be answered"),
    ({"metric": "volumes"}, "CANNOT be answered"),
    ({"metric": "containers"}, "CANNOT be matched"),
    ({"metric": "routers"}, "NOT the physical"),
    ({"metric": "alerts"}, "alerting rule"),
]
