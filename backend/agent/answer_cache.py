"""Short-lived cache of finished answers.

The daily token allowance is small and easily spent, and demos repeat the same
questions. Serving a repeat question from cache costs nothing and returns
instantly.

The TTL is deliberately short. This is monitoring data — an answer about which
hosts are down goes stale in seconds, so the cache exists to absorb repeats
within one conversation, not to remember anything.
"""

import hashlib
import json
import os
import threading
import time

DEFAULT_TTL_SECONDS = int(os.getenv("ANSWER_CACHE_TTL", "60"))
MAX_ENTRIES = 128

# Questions asking for a fresh reading should bypass a cached one.
_BYPASS_HINTS = ("refresh", "right now", "recheck", "re-check", "again",
                 "latest", "current status", "check again", "update")

_lock = threading.Lock()
_entries = {}
_stats = {"hits": 0, "misses": 0, "stores": 0, "bypasses": 0}


def _normalise(question: str) -> str:
    return " ".join(str(question or "").lower().split()).strip(" ?.!")


def build_key(question: str, conversation_history=None) -> str:
    """Cache key over the question plus the turns that give it meaning.

    "what about server02?" means different things depending on what preceded it,
    so history is part of the identity of the question, not context around it.
    """
    from agent.history import recent_turns

    payload = {
        "question": _normalise(question),
        "history": [f"{t['role']}:{_normalise(t['content'])[:200]}"
                    for t in recent_turns(conversation_history)],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def should_bypass(question: str) -> bool:
    text = _normalise(question)
    return any(hint in text for hint in _BYPASS_HINTS)


def get(question: str, conversation_history=None, ttl: int = None):
    """A cached payload, or None. Expired and bypassed reads both count as misses."""
    ttl = DEFAULT_TTL_SECONDS if ttl is None else ttl
    if ttl <= 0:
        return None
    if should_bypass(question):
        with _lock:
            _stats["bypasses"] += 1
        return None

    key = build_key(question, conversation_history)
    now = time.time()
    with _lock:
        entry = _entries.get(key)
        if entry and now - entry["stored_at"] < ttl:
            _stats["hits"] += 1
            payload = dict(entry["payload"])
            payload["cached"] = True
            payload["cache_age_seconds"] = round(now - entry["stored_at"], 1)
            return payload
        if entry:
            _entries.pop(key, None)
        _stats["misses"] += 1
    return None


def put(question: str, payload: dict, conversation_history=None) -> None:
    """Store a successful answer. Failures are never cached.

    Caching an error would keep serving a rate-limit message after the limit
    had cleared, turning a transient problem into a sticky one.
    """
    if not payload or payload.get("complexity") == "error":
        return
    if not str(payload.get("final_answer") or "").strip():
        return

    key = build_key(question, conversation_history)
    with _lock:
        if len(_entries) >= MAX_ENTRIES:
            oldest = min(_entries, key=lambda k: _entries[k]["stored_at"])
            _entries.pop(oldest, None)
        _entries[key] = {"payload": payload, "stored_at": time.time()}
        _stats["stores"] += 1


def stats() -> dict:
    with _lock:
        total = _stats["hits"] + _stats["misses"]
        return {**_stats,
                "entries": len(_entries),
                "ttl_seconds": DEFAULT_TTL_SECONDS,
                "hit_rate": round(_stats["hits"] / total, 3) if total else 0.0}


def clear() -> None:
    with _lock:
        _entries.clear()
