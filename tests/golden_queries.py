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
    # Asked from the start, answerable only since the metric vocabulary grew
    # beyond cpu/memory/traffic/disk. Uptime, OS version, model and serial come
    # from SNMP items, not from host inventory — inventory is disabled on every
    # host in this estate.
    ("uptime of a router", {"query_type": "items", "host_filter": "192.168.1.73",
                            "item_keyword": "uptime"}, "Uptime"),
    # The questions as actually asked are about a CLASS of device, not one host.
    # Mapping them to a single-device call made the eval pass while the real
    # question ("what is the uptime of routers/switches") returned nothing.
    ("uptime across a group", {"query_type": "items", "group_filter": "Routers",
                               "item_keyword": "uptime"}, "across 2 host(s)"),
    ("IOS version of a router", {"query_type": "items", "host_filter": "192.168.1.75",
                                 "item_keyword": "IOS version"}, "OS / software version"),
    ("IOS version across a group", {"query_type": "items", "group_filter": "Routers",
                                    "item_keyword": "IOS version"},
     "OS / software version"),
    ("model and serial", {"query_type": "items", "host_filter": "192.168.1.75",
                          "item_keyword": "model and serial"}, "Hardware (model / serial)"),
    ("agent version", {"query_type": "items", "host_filter": "Nikunj VM",
                       "item_keyword": "agent version"}, "Zabbix agent version"),
    ("ping latency", {"query_type": "items", "host_filter": "192.168.1.73",
                      "item_keyword": "ping latency"}, "ICMP ping"),
    # Zabbix holds no lifecycle data at all. The expectation is that the tool
    # says so rather than returning an empty table.
    ("EOL/EOS of a device", {"query_type": "items", "host_filter": "192.168.1.75",
                             "item_keyword": "EOL EOS"},
     "does not record end-of-life"),
    ("SLA for the estate", {"query_type": "sla"}, "SLA(s) configured"),
    ("SLA for one device", {"query_type": "sla", "host_filter": "192.168.1.74"},
     "against a"),
    ("SNMP-monitored devices", {"query_type": "hosts"}, "SNMP-monitored devices"),
    # Agent 1 vs Agent 2 cannot be told apart here: agent.variant is collected on
    # no host. The expectation is that the answer says so rather than guessing
    # from the version number.
    ("Zabbix Agent 2 count", {"query_type": "hosts"}, "does not distinguish Zabbix Agent 1"),
]

# The questions this assistant was originally specified against, kept as one
# list so coverage is checkable rather than remembered. Each maps to the entry
# in ZABBIX_QUERIES that answers it.
ZABBIX_SAMPLE_QUESTIONS = [
    ("Please share total hosts", "total hosts"),
    ("Please share total routers", "total routers"),
    ("Please share total switches", "total switches"),
    ("Please share the total devices which are up", "devices up"),
    ("Please share total devices which are down", "devices down"),
    ("What is the uptime of routers/switches/devices?", "uptime across a group"),
    ("What is the SLA for devices/router/switches", "SLA for the estate"),
    ("What is the EOL EOS for devices?", "EOL/EOS of a device"),
    ("What are the current IOS running in router/switches/devices?",
     "IOS version across a group"),
    ("Which host group has the highest number of down devices?",
     "host group with most down"),
    ("What is the current cpu & memory utilization of 192.168.1.73?",
     "cpu AND memory of a host"),
    ("List all hosts currently in 'unreachable' or 'unknown' status",
     "unreachable or unknown"),
    ("How many alerts are currently active?", "active problems"),
    ("What is the status of 192.168.1.75 host?", "status of one host"),
    ("How many active problems are there right now?", "active problems"),
    ("What is the network traffic of 192.168.1.73?", "network traffic"),
    ("How many host groups are configured in Zabbix?", "host group with most down"),
    ("How many SNMP-based devices are being monitored?", "SNMP-monitored devices"),
    ("How many alerts were sent today?", "alerts today"),
    ("How many devices are monitored through Zabbix Agent 2?", "Zabbix Agent 2 count"),
    ("How many triggers are currently enabled vs disabled?",
     "triggers enabled vs disabled"),
    ("Tell me the problem with {DEVICE IP/NAME}?", "problem with one device"),
    ("When was the config of {device} changed?", "config change of a device"),
    ("How many times is the config changed?", "config change of a device"),
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

# --- OpenSearch: OpenStack application logs ---------------------------------
OPENSEARCH_QUERIES = [
    ("logs for today", {}, "for today"),
    ("logs for yesterday", {"date_filter": "yesterday"}, "for yesterday"),
    ("logs for last 2 hours", {"date_filter": "last 2 hours"}, "last 2 hour"),
    ("logs for an exact date", {"date_filter": "2026-08-11"}, "on 2026-08-11"),
    ("full text search", {"search": "rabbit", "date_filter": "last 7 days"}, "matching 'rabbit'"),
    ("errors only", {"log_level": "error", "date_filter": "last 7 days"}, "level error"),
    ("one service", {"service": "nova-api"}, "service 'nova-api'"),
    ("one host", {"host_filter": "server02"}, "host 'server02'"),
    ("breakdown by service", {"group_by": "service"}, "BREAKDOWN BY SERVICE"),
    ("breakdown by hour", {"group_by": "hour"}, "BY HOUR"),
    ("breakdown by level", {"group_by": "level"}, "BREAKDOWN BY LEVEL"),
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
