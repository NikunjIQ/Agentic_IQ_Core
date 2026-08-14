"""Ask a specific question instead of dead-ending.

When the app could not work out what was meant it used to tell the user to
rephrase and "state the system and time period explicitly". That hands the
problem back to the person who already said what they wanted.

Instead it asks one question with real options in it — actual host groups from
Zabbix, actual ticket states from Zammad — drawn from the metadata cache, so a
clarifying question costs no API calls and no model calls.

The model only decides WHICH axis is unclear. The question text is built here,
from live values, so it can never offer a host group that does not exist.
"""

import re

from agent.metadata import live_options

AXES = ("system", "scope", "timeframe")

# Every clarifying question ends with this line. It doubles as the marker that
# lets the next turn recognise it was a clarification and not ask twice.
ESCAPE_OPTION = "something else — just tell me more"


def _bullets(options) -> str:
    return "\n".join(f"- {option}" for option in options)


def build(axis: str, question: str = "") -> str:
    """The clarifying question for one axis, using real values where possible."""
    options = live_options()

    if axis == "scope":
        groups = options.get("zabbix_groups") or []
        # Offer the groups that actually hold devices rather than the whole list.
        preferred = [g for g in groups
                     if g in ("Routers", "Switches", "Server", "Linux servers",
                              "Discovered hosts", "Website")]
        shown = (preferred or groups)[:4]
        if shown:
            return ("Which set of devices did you mean?\n"
                    + _bullets(shown + [ESCAPE_OPTION]))
        axis = "system"        # no groups available — fall back to the system axis

    if axis == "timeframe":
        return ("What time period should I look at?\n"
                + _bullets(["right now", "today", "the last 7 days", ESCAPE_OPTION]))

    # "system" is the default and the most common ambiguity: the two monitoring
    # systems watch completely different machines.
    nodes = options.get("prometheus_nodes") or []
    linux = f" ({', '.join(nodes)})" if nodes else ""
    groups = options.get("zabbix_groups") or []
    network = ""
    named = [g for g in groups if g in ("Routers", "Switches")]
    if named:
        network = f" ({', '.join(named)})"

    return ("I can check a few different systems — which did you mean?\n"
            + _bullets([
                f"network devices in Zabbix{network} — routers, switches, Windows hosts",
                f"the Linux servers in Prometheus{linux} — CPU, memory, disk, VMs",
                "helpdesk tickets in Zammad",
                ESCAPE_OPTION,
            ]))


def was_just_asked(conversation_history) -> bool:
    """True if the previous assistant turn was itself a clarifying question.

    Without this the app can ask, receive an answer it does not recognise as an
    answer, and ask again — which is worse than never asking at all.
    """
    if not conversation_history:
        return False
    for message in reversed(conversation_history):
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant":
            return ESCAPE_OPTION in str(message.get("content") or "")
    return False


# Words that anchor a question to something concrete — a system, a kind of
# object, a metric, a host. A question containing none of these ("what's down",
# "is everything ok") does not say enough to route on.
_ANCHOR = re.compile(
    r"""(?ix)\b(
        zabbix | prometheus | zammad | opensearch | grafana
      | ticket s? | helpdesk | sla | agent s? | escalat \w*
      | log s? | error s? | openstack | nova | neutron | keystone | glance | cinder
      | rabbit \w* | mysql | haproxy | elastic \w* | container s? | exporter s?
      | host s? | device s? | router s? | switch \w* | server \w* | node s?
      | vm s? | virtual \s+ machine s? | instance s?
      | cpu | memory | ram | disk | storage | filesystem | traffic | bandwidth
      | interface s? | uptime | load | trigger s? | alert s? | problem s?
      | windows | linux | snmp | ping | port s?
    )\b
    | \d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}
    """
)


def looks_ambiguous(question: str) -> bool:
    """True when the question names nothing that could be routed on.

    A deterministic floor under the model's judgement. The classifier is a small
    model and is easily talked out of asking — this catches the plainly vague
    cases regardless of what it decided.
    """
    return not _ANCHOR.search(str(question or ""))


def normalise_axis(value) -> str:
    """Accept only a known axis; anything else means 'do not ask'."""
    text = str(value or "").strip().lower()
    return text if text in AXES else ""
