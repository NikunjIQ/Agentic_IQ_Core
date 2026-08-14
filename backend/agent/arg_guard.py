"""Drop tool arguments the user never asked for.

The model fills blanks. Asked "How many tickets are in Open State?" — a question
with no time period in it — the extractor returned
date_filter='last 10 days', because that phrase appeared in the tool's own
examples. The result was a query over 2 tickets instead of 22.

A prompt can ask the model not to do that. This checks. The rules here are
deliberately generic: they know nothing about tickets, hosts or logs, only
whether the user's words support the argument being present. That keeps the
system open to questions nobody anticipated while stopping it inventing
constraints.
"""

import re

# Words that mean "a period of time". Deliberately excludes 'now', 'current' and
# 'currently': those describe a live state, not a window, and treating them as a
# window is what lets an unasked-for date filter back in.
_TIME_PATTERN = re.compile(
    r"""(?ix)
    \b(
        today | yesterday | tomorrow | tonight
      | (this|last|past|previous|next)\s+(week|month|year|quarter|day|hour|night)
      | \d+\s*(second|sec|minute|min|hour|hr|day|week|month|year)s?
      | week | month | quarter | year | daily | weekly | monthly | hourly
      | recent(ly)? | lately | since | ago | between | during | over\s+the
      | so\s+far | to\s+date | history | historical | trend | overnight
      | jan(uary)? | feb(ruary)? | mar(ch)? | apr(il)? | may | jun(e)? | jul(y)?
      | aug(ust)? | sep(tember)? | oct(ober)? | nov(ember)? | dec(ember)?
      | mon(day)? | tue(sday)? | wed(nesday)? | thu(rsday)? | fri(day)?
      | sat(urday)? | sun(day)?
    )\b
    | \d{4}-\d{1,2}-\d{1,2}
    | \d{1,2}/\d{1,2}/\d{2,4}
    """
)

# Arguments that only make sense if the user referred to a time period.
_TIME_ARGUMENTS = ("date_filter", "date_field", "range_window")


def mentions_a_time_period(*texts) -> bool:
    """True if any of the supplied text refers to a period of time."""
    return any(_TIME_PATTERN.search(str(text or "")) for text in texts)


def scrub(tool_args: dict, question: str, conversation_history=None) -> tuple:
    """Return (cleaned arguments, notes describing anything removed).

    Conversation history counts as evidence: after "show me logs for yesterday",
    a bare "and errors?" is still legitimately about yesterday.
    """
    if not isinstance(tool_args, dict) or not tool_args:
        return tool_args, []

    from agent.history import recent_turns

    context = [question] + [turn["content"] for turn in recent_turns(conversation_history)]
    if mentions_a_time_period(*context):
        return tool_args, []

    cleaned, removed = dict(tool_args), []
    for name in _TIME_ARGUMENTS:
        value = cleaned.get(name)
        if value not in (None, "", [], {}):
            # range_window has a genuine non-temporal use: it sizes the averaging
            # window for a rate, and is harmless left at its default. Only strip
            # it when it would restrict which rows are considered.
            if name == "range_window" and "metric" in cleaned:
                continue
            cleaned.pop(name, None)
            removed.append(f"{name}={value!r}")

    return cleaned, removed
