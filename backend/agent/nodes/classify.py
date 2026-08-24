import json
from langchain_groq import ChatGroq
from agent.state import AgentState
from agent.tool_registry import TOOL_REGISTRY
from agent.history import recent_turns
from agent import streaming, clarify, telemetry, vocabulary

from agent.models import FAST_MODEL, REASONING_MODEL

CLASSIFY_MODEL = FAST_MODEL

CLASSIFY_PROMPT = """You are a fast triage classifier for an IT operations assistant.
Given a user's question, decide:

1. Is this a SIMPLE question, answerable with exactly ONE call to exactly ONE tool?
   Examples: "how many hosts are down", "how many tickets are open"
2. Or is this a COMPLEX question requiring MULTIPLE tool calls and reasoning across
   more than one system? Examples: "are there tickets related to hosts that are down",
   "what's the status of host X and any related tickets"

   DECISIVE TEST for complexity: does answering require facts from TWO systems?
   A question that names a subject belonging to one system AND asks about tickets,
   or about anything held by a different system, is COMPLEX — even when it sounds
   like one short question. Naming a subject from another system is not a filter
   that one tool can apply.
   COMPLEX: "is there a ticket for the ceph network alert" (Ceph + Zammad),
            "is anyone working on the disk space problem" (Zabbix + Zammad),
            "which VM is slow and is it logging errors" (Prometheus + OpenSearch),
            "how much Ceph storage is jammy using" (Ceph + Prometheus — Ceph
            only knows raw UUIDs and pool names, NEVER a VM's name; resolving
            "jammy" to which Ceph image that is comes from Prometheus, making
            this two systems even though only "Ceph" is said aloud),
            "is the windows VM's disk on ceph" (same reason — a VM name paired
            with anything about Ceph storage always needs Prometheus first).
   SIMPLE:  "how many tickets are open" (Zammad alone),
            "is ceph healthy" (Ceph alone),
            "how many OSDs are up" (Ceph alone — a storage object names its
            system just as surely as the word Ceph does),
            "how much storage does the volumes pool use" (Ceph alone — a POOL
            name is Ceph's own vocabulary, unlike a VM name, which is not).

   COMPLEXITY IS NOT CONFIDENCE. It asks how many systems hold the facts, and
   nothing else. A question you find hard to place is still SIMPLE if one system
   holds the answer. Never answer "complex" because you could not pick a tool.

Available tools:
{tool_descriptions}

   Some words belong to two systems. These are settled for this estate — apply
   the default unless the question carries one of the signals beside it, and
   apply the same reasoning to wordings not listed here:
{collisions}

   PICK A TOOL. Abstaining is not the safe choice: it costs the user an answer.
   If two tools could serve, choose the likelier and move on.

3. Is the question too vague to act on without guessing? If so, name the ONE axis that
   is unclear:
   - "system"    — cannot tell which system is meant (Zabbix network devices vs
                   Prometheus Linux servers vs Zammad tickets vs OpenSearch logs)
   - "scope"     — cannot tell which devices or group
   - "timeframe" — cannot tell what period
   Otherwise null.

   Set "ambiguous" to a value ONLY when the question names nothing to route on.
   Ambiguous: "what's down", "what's wrong", "is everything ok", "any issues",
   "show me the status" — no system, device, or object type is mentioned.

   Set it to null whenever the question names ANY of: a system (Zabbix, Prometheus,
   Zammad, OpenSearch, Ceph), an object type (hosts, tickets, VMs, logs, containers,
   pools, OSDs, RBD images, snapshots, CephFS), a device or IP, a host group, or a
   metric (CPU, memory, disk). An object type names its system implicitly — only
   Ceph has snapshots and OSDs, so "give me the snapshot details" is NOT ambiguous.
   Also null if the previous assistant message was a clarifying question — the
   user is answering it, not asking something new.

   Being unsure WHICH system is not the same as the question naming none. A
   question that names an object type is answerable; ask only when the sentence
   names nothing at all.

Call the `route` tool with your decision. Do not write JSON as your reply —
the tool call itself carries the answer.
"""

def _route_schema() -> dict:
    """The shape a routing decision must have, built from the live registry.

    Argument extraction forces its schema with tool_choice; classification
    parsed free text and fell into `except` on anything unexpected, quietly
    becoming "complex" with no tool. Two failures hid in there: a malformed
    reply read as an expensive question rather than an error, and an invented
    tool name — nothing checked the string against the registry — reached
    simple_path, which found no such tool and asked the user which system they
    meant. Enumerating the names here makes both impossible to express.
    """
    return {
        "type": "function",
        "function": {
            "name": "route",
            "description": "Record where this question should go.",
            "parameters": {
                "type": "object",
                "properties": {
                    "complexity": {
                        "type": "string", "enum": ["simple", "complex"],
                        "description": "How many systems hold the facts, not "
                                       "how confident you are."},
                    "tool": {
                        "type": "string",
                        "enum": sorted(TOOL_REGISTRY) + ["none"],
                        "description": "The one tool that answers it, or 'none' "
                                       "when the question spans two systems."},
                    "ambiguous": {
                        "type": "string",
                        "enum": ["system", "scope", "timeframe", "none"],
                        "description": "'none' unless the question names nothing "
                                       "at all to route on."},
                },
                "required": ["complexity", "tool", "ambiguous"],
            },
        },
    }


def _read_decision(response) -> tuple:
    """(complexity, tool, axis) from a forced call, or from loose JSON.

    The JSON branch is kept for the case where tool_choice is refused, so a
    provider quirk degrades to the old behaviour instead of failing the
    request. Returns None when neither shape is readable, which the caller
    reports rather than swallows.
    """
    calls = getattr(response, "tool_calls", None) or []
    if calls:
        args = calls[0].get("args") or {}
    else:
        try:
            args = json.loads(getattr(response, "content", "") or "")
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(args, dict):
        return None

    tool = args.get("tool")
    if tool in ("none", "null", ""):
        tool = None
    # The enum makes this unreachable on the forced path; the JSON fallback has
    # no such guarantee, and an unknown name costs the user an answer.
    if tool is not None and tool not in TOOL_REGISTRY:
        tool = None
    return (args.get("complexity") or "complex", tool,
            clarify.normalise_axis(args.get("ambiguous")))


def _classify_node_impl(state: AgentState) -> AgentState:
    streaming.status("Working out which system to check…")
    # Routing summaries, not full docstrings. This node picks a tool name; the
    # parameter documentation only matters once arguments are being extracted.
    tool_descriptions = "\n".join(f"- {name}: {entry['routing_summary']}"
                                  for name, entry in TOOL_REGISTRY.items())
    # Shared nouns come from agent.vocabulary in their short form: the model
    # needs the rule, not the reasoning behind it, and this is already the most
    # expensive call in the app.
    prompt = CLASSIFY_PROMPT.format(tool_descriptions=tool_descriptions,
                                    collisions=vocabulary.collision_guidance())

    # A follow-up ("and server02?") is unroutable on its own — the system it
    # belongs to lives in the previous turn.
    messages = ([{"role": "system", "content": prompt}]
                + recent_turns(state.get("conversation_history"), max_turns=4)
                + [{"role": "user", "content": state["question"]}])

    # The small model occasionally emits a tool call Groq rejects outright as
    # malformed (tool_use_failed) — a generation glitch, not a comprehension
    # failure. simple_path.py already retries its own forced call with the
    # larger model for exactly this reason; this call had no such protection
    # until a real question hit it, surfaced as "could not build a valid query
    # for it" while the graph shows only classify ever having run — no tool
    # was ever chosen, so there is nothing to blame on routing or on Ceph.
    decision = None
    for model, escalate in ((CLASSIFY_MODEL, False), (REASONING_MODEL, True)):
        try:
            with telemetry.measure("classify", model,
                                   "escalated" if escalate else "") as call:
                call.response = response = ChatGroq(
                    model=model, temperature=0
                ).bind_tools([_route_schema()], tool_choice="route").invoke(messages)
            decision = _read_decision(response)
            if decision is not None:
                break
        except Exception as exc:
            if "tool_use_failed" not in str(exc) and "400" not in str(exc):
                raise      # rate limits and outages should surface, not retry

    if decision is None:
        # Still fail safe into the complex path, but say so. This used to be
        # silent, so a prompt change that broke parsing would show up as every
        # question suddenly costing several times as much, with no error to
        # explain it.
        streaming.status("Could not read the routing decision — checking more "
                         "than one system to be safe")
        decision = ("complex", None, "")

    state["complexity"], state["simple_tool_choice"], axis = decision

    # Never ask twice in a row. If the previous turn was already a clarifying
    # question, this message is the answer to it — asking again would trap the
    # user in a loop, which is worse than guessing.
    if axis and clarify.was_just_asked(state.get("conversation_history")):
        axis = ""
    # A deterministic floor under the classifier. The small model is easily
    # talked out of asking — "show me what's down" reached Zabbix because an
    # example in this prompt told it broad questions were answerable. Code
    # catches the plainly vague cases whatever the model decided.
    elif not axis and clarify.looks_ambiguous(state["question"])             and not clarify.was_just_asked(state.get("conversation_history")):
        axis = "system"
    # And a ceiling over it, in the other direction. The same small model also
    # over-asks: "Give me the snapshot details" is an official Ceph question and
    # came back as "which system did you mean?", because no system is named in
    # the sentence even though only one owns the word "snapshot". When the
    # question's vocabulary belongs to exactly one system, that is not ambiguity
    # — and a question naming two systems still falls through to the ask.
    if axis and not state.get("simple_tool_choice"):
        resolved = clarify.resolves_to_one_tool(state["question"])
        if resolved:
            state["simple_tool_choice"] = resolved
            state["complexity"] = "simple"
            axis = ""

    # Last word, deliberately: a real tool from the model's own forced call
    # outranks any axis, including one the floor above just set. "What is the
    # current unresolved case count" named no anchor word, so the floor
    # reasserted axis="system" — but the SAME response had already named
    # query_zammad correctly. The schema does not force `tool` and
    # `ambiguous` to agree with each other, so this is the one place that
    # does: a tool the model could only pick from a real enum is stronger
    # evidence than a guess about ambiguity made in the same breath. This
    # must run after the floor, not before it — clearing axis earlier would
    # just have the floor put it right back, since the floor's whole job is
    # to fire when the raw text matches no known word, which is what a
    # genuine paraphrase looks like by definition.
    if axis and state.get("simple_tool_choice"):
        axis = ""

    state["clarify_axis"] = axis

    return state


def classify_node(state: AgentState) -> AgentState:
    """Run the node, bracketed by the events that let a caller draw the run."""
    with streaming.node("classify"):
        return _classify_node_impl(state)
