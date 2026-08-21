"""One place for the words that say what each system answers.

The classifier reads a one-line summary of each tool, not its docstring. When
Ceph was added and Zammad widened, those summaries were not updated, so the word
"snapshot" appeared nowhere the classifier could see and snapshot questions were
routed to Zabbix. The same vocabulary had been copied into four files by then,
each drifting on its own; every routing bug found in that session came from one
of them being behind.

This module is the source those places check against. Two rules keep it honest:

**It documents, it does not decide.** Nothing here routes a question. The words
are used to check that the summaries and prompts are complete, and to recognise
that a question names *something* — never to choose the system. The model makes
every routing call, so a phrasing nobody listed still works. A list that decides
is static; a list that documents is not.

**Everything a tool can answer needs an entry.** `SURFACE` is keyed by each
tool's own surface — its `query_type` values where it has them, its parameters
where it does not. A test walks that surface and fails when something is
reachable in code but unsayable in the summary, which is the exact shape of the
snapshot bug.
"""

# ---------------------------------------------------------------- the surface
# tool -> capability -> the words a person would actually use for it.
# At least one word per capability must appear in that tool's routing summary.

SURFACE = {
    "query_ceph": {
        "health": ("health", "status"),
        "capacity": ("capacity", "free space"),
        "pools": ("pools",),
        "osds": ("OSDs",),
        "hosts": ("cluster hosts",),
        "images": ("RBD images",),
        "snapshots": ("snapshots",),
        "cephfs": ("CephFS", "MDS"),
        "crush": ("CRUSH", "replication"),
        "logs": ("cluster logs", "disk failures"),
        # Ceph's dashboard records where its own monitoring lives, so "get me
        # the Grafana URL" is a Ceph question and not a Prometheus one.
        "endpoints": ("monitoring endpoints", "Grafana", "Prometheus URL"),
        "rgw": ("object storage", "RGW", "S3 buckets"),
    },
    "query_zabbix": {
        "hosts": ("hosts", "devices", "up/down status"),
        "hostgroups": ("host groups",),
        "items": ("device metrics", "CPU", "memory", "traffic", "uptime"),
        "problems": ("problems",),
        "alerts": ("alerts",),
        "triggers": ("triggers",),
        "audit": ("config audit", "config changes"),
        "sla": ("SLAs", "service levels"),
    },
    "query_zammad": {
        "state_filter": ("tickets", "states"),
        "priority_filter": ("priorities",),
        "owner_filter": ("agents",),
        "organization_filter": ("organisations",),
        "group_by": ("group_by breakdowns",),
        "tag_filter": ("tags",),
        "search": ("free-text search",),
        "overview": ("named queues",),
        "ticket_reference": ("one ticket's conversation",),
        "sla": ("SLA", "response times"),
        "time_accounting": ("booked time",),
    },
    "query_prometheus": {
        "cpu": ("CPU",),
        "memory": ("memory",),
        "disk": ("disk",),
        "load": ("load",),
        "uptime": ("uptime",),
        "network": ("network",),
        "targets": ("scrape targets",),
        "vms": ("VMs",),
        "openstack": ("OpenStack routers", "networks"),
        "volumes": ("Cinder volume", "snapshot COUNTS"),
        "containers": ("containers",),
        "promql": ("PromQL",),
    },
    "query_opensearch": {
        "date_filter": ("logs",),
        "search": ("application logs",),
        "log_level": ("flog-*",),
        "service": ("OpenStack",),
        "host_filter": ("OpenStack",),
        "group_by": ("logs",),
    },
}

# Parameters that shape a query rather than name something to ask for. A user
# never says "range_window", so requiring vocabulary for it would force noise
# into the summaries. Listed explicitly so skipping one is a decision.
PLUMBING = {
    "limit", "date_field", "range_window", "host_filter", "job_filter",
    "customer_filter", "tenant", "promql", "question",
}


# ------------------------------------------------------------- shared nouns
# Words that two systems both own. Neither answer is wrong on the words alone,
# so the estate has to decide — and a decision nobody writes down is a coin
# toss. `prefer` is what this deployment means when nothing else disambiguates;
# `signals` are the co-words that override it.
#
# These reach the model as stated facts in the classify prompt, not as `if`
# statements in the routing path, so it can apply the idea to phrasings that do
# not literally match.

COLLISIONS = (
    {"noun": "hosts",
     "systems": ("query_zabbix", "query_ceph"),
     "prefer": "query_zabbix",
     "signals": {"query_ceph": ("ceph", "cluster", "osd", "storage"),
                 "query_prometheus": ("server01", "server02", "server03")},
     "note": "Zabbix watches the network estate; Ceph has three cluster hosts."},

    {"noun": "routers",
     "systems": ("query_zabbix", "query_prometheus"),
     "prefer": "query_zabbix",
     "signals": {"query_prometheus": ("neutron", "openstack", "tenant")},
     "note": "Cisco kit unless OpenStack is named, when it means neutron."},

    {"noun": "snapshots",
     "systems": ("query_ceph", "query_prometheus"),
     "prefer": "query_ceph",
     "signals": {"query_prometheus": ("cinder", "openstack")},
     "note": "RBD snapshots are Ceph; Prometheus only counts Cinder's."},

    {"noun": "volumes",
     "systems": ("query_ceph", "query_prometheus"),
     "prefer": "query_ceph",
     "signals": {"query_prometheus": ("cinder", "openstack")},
     "note": "The volumes pool is Ceph's; the Cinder count is Prometheus's."},

    {"noun": "logs",
     "systems": ("query_opensearch", "query_ceph"),
     "prefer": "query_opensearch",
     "signals": {"query_ceph": ("ceph", "cluster", "osd", "disk failure")},
     "note": "OpenSearch holds OpenStack service logs; Ceph has its own."},

    {"noun": "uptime",
     "systems": ("query_zabbix", "query_prometheus"),
     "prefer": "query_zabbix",
     "signals": {"query_prometheus": ("server01", "server02", "server03", "vm")},
     "note": "Network kit unless a Linux node or VM is named."},

    {"noun": "windows",
     "systems": ("query_zabbix", "query_prometheus"),
     "prefer": "query_zabbix",
     "signals": {"query_prometheus": ("vm", "instance", "openstack")},
     "note": "Zabbix host Windows at .104 and OpenStack VM windows at 10.0.0.41 "
             "are different machines."},

    # Both found by the routing score: the classifier abstained on "how many
    # alerts are currently active?" and "how many errors were logged today?"
    # because two summaries each plausibly claimed the word and nothing said
    # which wins.
    {"noun": "alerts",
     "systems": ("query_zabbix", "query_ceph"),
     "prefer": "query_zabbix",
     "signals": {"query_ceph": ("ceph", "storage", "osd", "pool")},
     "note": "Zabbix raises the estate's alerts; Ceph only has storage ones."},

    {"noun": "errors",
     "systems": ("query_opensearch", "query_zabbix"),
     "prefer": "query_opensearch",
     "signals": {"query_zabbix": ("device", "router", "switch", "snmp")},
     "note": "Logged errors are OpenSearch; a device fault is a Zabbix problem."},

    {"noun": "grafana url",
     "systems": ("query_ceph", "query_prometheus"),
     "prefer": "query_ceph",
     "signals": {},
     "note": "Ceph's dashboard records where its monitoring lives. Same for "
             "the Prometheus URL — asking for it is a Ceph question."},
)


def collision_guidance(compact: bool = True) -> str:
    """The shared nouns, written for the classify prompt.

    Compact by default. Classification is already the most expensive operation
    in the app, and the model needs the rule, not the reasoning behind it — the
    `note` on each entry is there for whoever reads this file next. Spelling it
    all out costs ~367 tokens against a ~1,034-token call; the short form is
    under half that.
    """
    lines = []
    for entry in COLLISIONS:
        default = entry["prefer"].replace("query_", "")
        overrides = "; ".join(
            f"{tool.replace('query_', '')} if {'/'.join(words)}"
            for tool, words in entry["signals"].items() if words)
        if compact:
            line = f'  "{entry["noun"]}" -> {default}'
            if overrides:
                line += f" ({overrides})"
        else:
            systems = " and ".join(s.replace("query_", "") for s in entry["systems"])
            line = f'- "{entry["noun"]}" is claimed by {systems}; default to {default}'
            if overrides:
                line += " unless " + overrides
            line += f". {entry['note']}"
        lines.append(line)
    return "\n".join(lines)


# ------------------------------------------------------------ context words
# Words that make a question routable without naming a capability: the systems
# themselves, the OpenStack services underneath them, and the everyday synonyms
# people reach for. A question containing none of these and none of the surface
# vocabulary ("what's down", "is everything ok") names nothing to route on.

CONTEXT_WORDS = (
    # the systems, by name
    "zabbix", "prometheus", "zammad", "opensearch", "ceph", "grafana",
    # OpenStack, whose services appear in logs and metrics
    "openstack", "nova", "neutron", "keystone", "glance", "cinder",
    "rabbitmq", "mysql", "haproxy", "elasticsearch", "exporter",
    # the machines, however they get described
    "device", "server", "node", "switch", "router", "machine", "instance",
    "vm", "virtual", "linux", "windows", "interface", "port",
    # what gets measured
    "cpu", "memory", "ram", "disk", "storage", "filesystem", "traffic",
    "bandwidth", "latency", "ping", "snmp", "load", "uptime",
    # what goes wrong, and who hears about it
    "error", "alert", "problem", "trigger", "ticket", "helpdesk", "sla",
    "agent", "escalated", "backup", "capacity",
)

# Matched as prefixes because their endings vary too much to list.
CONTEXT_STEMS = ("escalat", "replicat", "monitor", "utilis", "utiliz")

# Surface words too generic to anchor a question on their own. "status" is how
# Ceph's health branch is described, but "show me the status" names no system
# and must still get a question back. These stay in SURFACE — they are the
# right words for the summaries — and are kept out of the ambiguity floor only.
NOT_AN_ANCHOR = {
    "status", "health", "free", "space", "counts", "times", "levels",
    "changes", "states", "response", "breakdowns", "one", "service",
    "named", "search", "object", "monitoring", "details",
}


def anchor_words() -> tuple:
    """Every word that makes a question routable at all.

    Feeds the ambiguity floor in clarify.py, which asks a question back only
    when a sentence names nothing. That floor once did not know the word "ceph",
    so "is ceph healthy?" was answered with "which system did you mean?".
    Deriving it here means a new capability widens the floor on its own.
    """
    words = set(CONTEXT_WORDS)
    for capabilities in SURFACE.values():
        for phrases in capabilities.values():
            for phrase in phrases:
                words.update(part.lower() for part in phrase.split()
                             if len(part) > 2 and part.isalpha())
    words.update(entry["noun"] for entry in COLLISIONS)
    return tuple(sorted(words - NOT_AN_ANCHOR))


def anchor_pattern() -> str:
    """The ambiguity floor as one regex: any anchor word, or an IP address.

    Built rather than written out. The word boundaries went missing once when
    they were typed by hand — `\\b` is a valid string escape and became a
    backspace character, leaving a pattern that silently matched nothing.
    """
    words = "|".join(sorted(anchor_words(), key=len, reverse=True))
    stems = "|".join(CONTEXT_STEMS)
    # (?:es|s)? rather than s?, or "switch" fails to match "switches" and half
    # the network estate stops being routable.
    return (r"\b(?:" + words + r")(?:es|s)?\b"
            r"|\b(?:" + stems + r")\w*"
            r"|\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}")
