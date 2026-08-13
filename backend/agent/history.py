"""Shared handling of prior conversation turns.

Every node that talks to the model needs the same view of the conversation, and
the same guard against it growing without bound. The token budget is small
(12k/minute on the current Groq tier), so history is trimmed rather than passed
whole — an unbounded transcript is the fastest way to a 429.
"""

MAX_TURNS = 6  # 3 user/assistant exchanges
MAX_CHARS_PER_TURN = 1500


def recent_turns(conversation_history, max_turns: int = MAX_TURNS) -> list:
    """The last few turns, normalised and length-capped.

    Answers get long (a correlation table can run to thousands of characters),
    so each turn is truncated too. Losing the tail of an old answer costs far
    less than losing the whole conversation to a rate limit.
    """
    if not conversation_history:
        return []

    turns = []
    for message in conversation_history[-max_turns:]:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")
        if role not in ("user", "assistant") or not content:
            continue
        text = str(content)
        if len(text) > MAX_CHARS_PER_TURN:
            text = text[:MAX_CHARS_PER_TURN] + " …[truncated]"
        turns.append({"role": role, "content": text})
    return turns


def followup_context(conversation_history, max_turns: int = MAX_TURNS) -> str:
    """A compact transcript for prompts that take a single user message.

    The synthesis step sends one composed user message rather than a message
    list, so it needs the history inline to resolve "what about server02?".
    """
    turns = recent_turns(conversation_history, max_turns)
    if not turns:
        return ""
    lines = [f"{turn['role']}: {turn['content']}" for turn in turns]
    return "\n".join(lines)
