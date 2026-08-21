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

from agent import vocabulary
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
                "storage capacity and pools in Ceph",
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
#
# Derived from agent.vocabulary rather than kept here, because this list and
# the routing summaries drifted apart independently: the floor did not know
# the word "ceph", so "is ceph healthy?" was answered with a clarifying
# question that did not even offer Ceph. A new capability now widens the
# floor on its own.
_ANCHOR = re.compile(vocabulary.anchor_pattern(), re.IGNORECASE)


def looks_ambiguous(question: str) -> bool:
    """True when the question names nothing that could be routed on.

    A deterministic floor under the model's judgement. The classifier is a small
    model and is easily talked out of asking — this catches the plainly vague
    cases regardless of what it decided.
    """
    return not _ANCHOR.search(str(question or ""))


# Vocabulary that belongs to exactly ONE system. The classifier is a small model
# and over-asks in the other direction from looks_ambiguous: "Give me the
# snapshot details" is an official Ceph question, and it was coming back as
# "which system did you mean?" because no system is named in the sentence.
#
# Every shared noun is excluded. Scored against the routing corpus, a version
# that ignored them would have misrouted "Get me Prometheus URL?" to Prometheus
# — a Ceph question, since Ceph's dashboard is what records that URL — and sent
# two cross-system questions to Zammad alone. A word two systems claim is
# ambiguous by definition; the model applies the recorded default from the
# prompt, which can weigh a whole sentence, where this can only match a word.
_TOOL_WORDS = {
    "query_ceph": ("ceph", "cephfs", "osds?", "rbd", "crush", "mds",
                   "pools?", "replicas?", "replication"),
    "query_zabbix": ("zabbix", "snmp", "triggers?", "icmp"),
    # "tickets" earns its place by making cross-system questions match TWO
    # systems and so resolve to neither. Dropping it sent "is there a ticket for
    # the ceph network alert?" to Ceph alone. "problems" was tried here for the
    # same reason and removed: it pulled "is anyone working on the disk space
    # problem?" to Zabbix, which is the correlation question, not a Zabbix one.
    "query_zammad": ("zammad", "helpdesk", "queues?", "tickets?"),
    "query_prometheus": ("promql", "scrapes?", "exporters?", "containers?"),
    "query_opensearch": ("opensearch", "flog"),
}

# Compiled from the word lists rather than written out by hand, so the word
# boundaries cannot go missing: a routing rule that fires on a substring is
# worse than no rule at all.
_TOOL_VOCABULARY = tuple(
    (name, re.compile(r"\b(?:" + "|".join(words) + r")\b", re.IGNORECASE))
    for name, words in _TOOL_WORDS.items()
)

# Any question carrying one of these is left to the model, whatever else it says.
_SHARED_NOUNS = re.compile(
    r"\b(?:" + "|".join(re.escape(entry["noun"]) for entry in vocabulary.COLLISIONS)
    + r")\b", re.IGNORECASE)


def resolves_to_one_tool(question: str) -> str:
    """The single tool this question's vocabulary names, or "".

    A ceiling over the model's judgement, the counterpart to looks_ambiguous.
    Returns "" when two systems' words both appear, or when any shared noun is
    present — both are real ambiguity, and neither is this function's to settle.
    """
    text = str(question or "")
    if _SHARED_NOUNS.search(text):
        return ""
    matched = {name for name, pattern in _TOOL_VOCABULARY if pattern.search(text)}
    return matched.pop() if len(matched) == 1 else ""


def normalise_axis(value) -> str:
    """Accept only a known axis; anything else means 'do not ask'."""
    text = str(value or "").strip().lower()
    return text if text in AXES else ""

