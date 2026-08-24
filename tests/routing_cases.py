"""Which system should answer each question.

The classifier is the most expensive step in the app and nothing measured
whether it was right, so its mistakes were found by users. This is the corpus
that scores it.

Three kinds of case, because they mean different things:

  ROUTING_CASES     one correct system. Getting these wrong is a bug.
  COLLISION_CASES   more than one defensible answer, because two systems own
                    the same noun. Scored separately: they say more about the
                    estate than about the classifier, and counting them as
                    failures would hide the real ones.
  SHOULD_ASK        names nothing routable. Answering these is the bug.

The Zabbix and Ceph questions are the ones the team actually asked, lifted from
golden_queries. The other three systems had no natural-language questions at
all — only internal labels like "opened last 10 days", which nobody would type —
so those are written here.
"""

# --------------------------------------------------------------- one right answer
# (question, expected tool, expected complexity)

CEPH_ROUTING = [
    ("Crush rule", "query_ceph", "simple"),
    ("Give me the snapshot details", "query_ceph", "simple"),
    ("Give me MDS Details", "query_ceph", "simple"),
    ("How many client in ceph fs?", "query_ceph", "simple"),
    ("How many snapshot are saved and when was the last snapshot taken?",
     "query_ceph", "simple"),
    ("What are the Replication Details and status?", "query_ceph", "simple"),
    ("What is the current CEPH status?", "query_ceph", "simple"),
    ("Give me the CEPH OSD Treemap", "query_ceph", "simple"),
    ("What is the CEPH OSD Status?", "query_ceph", "simple"),
    ("What are the R-G-W roles configured?", "query_ceph", "simple"),
    ("How much storage is used and how much is free?", "query_ceph", "simple"),
    ("How many OSDs are HDD and how many are SDD?", "query_ceph", "simple"),
    ("What are the CEPH OSD Pool details", "query_ceph", "simple"),
    ("Give me information about rbd", "query_ceph", "simple"),
    ("Give me details of How many images, type and usage details",
     "query_ceph", "simple"),
    # The two that read like Prometheus questions and are not: Ceph's dashboard
    # records where its monitoring lives, and that is what is being asked for.
    ("Get me Prometheus URL?", "query_ceph", "simple"),
    ("Get me Graphana URL", "query_ceph", "simple"),
]

ZABBIX_ROUTING = [
    ("Please share total routers", "query_zabbix", "simple"),
    ("Please share total switches", "query_zabbix", "simple"),
    ("Please share the total devices which are up", "query_zabbix", "simple"),
    ("Please share total devices which are down", "query_zabbix", "simple"),
    ("What is the SLA for devices/router/switches", "query_zabbix", "simple"),
    ("What is the EOL EOS for devices?", "query_zabbix", "simple"),
    ("What are the current IOS running in router/switches/devices?",
     "query_zabbix", "simple"),
    ("Which host group has the highest number of down devices?",
     "query_zabbix", "simple"),
    ("What is the current cpu & memory utilization of 192.168.1.73?",
     "query_zabbix", "simple"),
    ("List all hosts currently in 'unreachable' or 'unknown' status",
     "query_zabbix", "simple"),
    ("How many alerts are currently active?", "query_zabbix", "simple"),
    ("What is the status of 192.168.1.75 host?", "query_zabbix", "simple"),
    ("How many active problems are there right now?", "query_zabbix", "simple"),
    ("What is the network traffic of 192.168.1.73?", "query_zabbix", "simple"),
    ("How many host groups are configured in Zabbix?", "query_zabbix", "simple"),
    ("How many SNMP-based devices are being monitored?", "query_zabbix", "simple"),
    ("How many alerts were sent today?", "query_zabbix", "simple"),
    ("How many devices are monitored through Zabbix Agent 2?",
     "query_zabbix", "simple"),
    ("How many triggers are currently enabled vs disabled?",
     "query_zabbix", "simple"),
    ("Tell me the problem with 192.168.1.73", "query_zabbix", "simple"),
    ("When was the config of 192.168.1.74 changed?", "query_zabbix", "simple"),
]

ZAMMAD_ROUTING = [
    ("How many tickets are open?", "query_zammad", "simple"),
    ("Which agent has the most tickets assigned?", "query_zammad", "simple"),
    ("How many tickets came in this week?", "query_zammad", "simple"),
    ("Which tickets are escalated?", "query_zammad", "simple"),
    ("Show me the tickets tagged network", "query_zammad", "simple"),
    ("Which organisation raises the most tickets?", "query_zammad", "simple"),
    ("What is our SLA attainment on the helpdesk?", "query_zammad", "simple"),
    ("How much time has been booked against tickets?", "query_zammad", "simple"),
]

PROMETHEUS_ROUTING = [
    ("What is the CPU usage of server01?", "query_prometheus", "simple"),
    ("How many VMs are running?", "query_prometheus", "simple"),
    ("Which VM is using the most memory?", "query_prometheus", "simple"),
    ("How many scrape targets are configured?", "query_prometheus", "simple"),
    ("Show me the OpenStack neutron routers", "query_prometheus", "simple"),
    ("How much disk is free on server02?", "query_prometheus", "simple"),
    ("What containers are running?", "query_prometheus", "simple"),
    ("What is the load average across the Linux cluster?",
     "query_prometheus", "simple"),
]

OPENSEARCH_ROUTING = [
    ("How many errors were logged today?", "query_opensearch", "simple"),
    ("Show me the OpenStack logs for yesterday", "query_opensearch", "simple"),
    ("Which service is logging the most errors?", "query_opensearch", "simple"),
    ("Search the logs for authentication failures", "query_opensearch", "simple"),
    ("Break down today's log volume by hour", "query_opensearch", "simple"),
    ("What is nova logging right now?", "query_opensearch", "simple"),
]

# Two systems, so the only correct complexity is complex. The tool is left as
# None for most of these: the orchestrator picks its own inside the loop, and
# naming one here would assert something this node does not decide. The two
# VM-storage cases are the exception — one tool answers each in a single call,
# classify reliably names it even while marking the question complex, and that
# is worth checking directly rather than only checking the complexity label.
CROSS_SYSTEM_ROUTING = [
    ("Is anyone working on the disk space problem?", None, "complex"),
    ("Is there a ticket for the ceph network alert?", None, "complex"),
    ("Which VM is slow and is it logging errors?", None, "complex"),
    ("Which problems have no ticket raised?", None, "complex"),
    ("What is the status of 192.168.1.73 and any related tickets?",
     None, "complex"),
    ("We are low on storage — has anyone raised it?", None, "complex"),
    # Ceph only knows raw UUIDs and pool names, never a VM's name — resolving
    # one to a Ceph image is Prometheus's job. Found live: this misrouted to
    # query_ceph alone, which silently dropped the VM name and answered with
    # cluster-wide capacity instead, with nothing telling the user it had
    # ignored what they asked about.
    ("How much Ceph storage is jammy using?", "correlate_vm_storage_and_compute",
     "complex"),
    ("How much storage does eve-ng use on ceph?", "correlate_vm_storage_and_compute",
     "complex"),
]

ROUTING_CASES = (CEPH_ROUTING + ZABBIX_ROUTING + ZAMMAD_ROUTING
                 + PROMETHEUS_ROUTING + OPENSEARCH_ROUTING + CROSS_SYSTEM_ROUTING)


# ------------------------------------------------------------- no single right answer
# (question, tools that are defensible, the one the corpus assumes)
#
# These are homonyms across systems. Both corpora claim "hosts" — Ceph's sample
# questions map "What are the Host Details?" to the storage cluster while
# Zabbix's map "total hosts" to the network estate, and neither sentence carries
# anything to tell them apart. Recording the preferred answer makes it a
# decision somebody made rather than an accident.

COLLISION_CASES = [
    ("What are the Host Details?", ("query_ceph", "query_zabbix"), "query_ceph"),
    ("Please share total hosts", ("query_zabbix", "query_ceph"), "query_zabbix"),
    ("What is the uptime of routers/switches/devices?",
     ("query_zabbix", "query_prometheus"), "query_zabbix"),
    ("Show me the volumes", ("query_ceph", "query_prometheus"), "query_ceph"),
    ("How many snapshots are there?",
     ("query_ceph", "query_prometheus"), "query_ceph"),
    ("Show me the logs", ("query_opensearch", "query_ceph"), "query_opensearch"),
    ("Are there any disk failures and what are logs?",
     ("query_ceph", "query_opensearch"), "query_ceph"),
    ("Is the windows machine busy?",
     ("query_zabbix", "query_prometheus"), "query_zabbix"),
    ("How many times is the config changed?",
     ("query_zabbix", "query_ceph"), "query_zabbix"),
]


# --------------------------------------------------------------- must ask instead
# Nothing here names a system, a device, an object type or a metric. Answering
# is the failure; a question back is the correct behaviour.

SHOULD_ASK = [
    "is it down?",
    "what's wrong?",
    "is everything ok?",
    "show me the status",
    "any issues?",
    "what happened?",
]


# Asked twice in a scoring run to count how many answers move. The classifier
# ran at temperature 0 and still gave "Give me the snapshot details" a different
# route on two consecutive turns, so instability is measured rather than assumed.
STABILITY_SAMPLE = [
    "Give me the snapshot details",
    "What are the Host Details?",
    "Please share total hosts",
    "How many tickets are open?",
    "Is anyone working on the disk space problem?",
    "How many VMs are running?",
]
