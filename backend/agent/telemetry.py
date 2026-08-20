"""Where a question's tokens went.

The models are capped at 8,000 tokens per minute, so "that felt expensive" needs
to become a number before anything can be optimised. Every figure here comes
from Groq's own `usage_metadata` on the response — nothing is estimated.

One recorder per request, carried on a context variable exactly as the streaming
emitter is, because the graph runs in a worker thread and the nodes should not
have to thread a collector through the state to reach it.

Reasoning tokens are broken out separately. gpt-oss models spend output tokens
thinking before they answer, and that work is invisible in the reply while still
being billed — on a short classification call it was 22 of 32 output tokens.
"""

import contextlib
import contextvars
import time

_current = contextvars.ContextVar("iqcore_telemetry", default=None)

# What each operation is for, so a reader does not have to know the graph.
OPERATION_LABELS = {
    "classify": "Deciding which system to check",
    "extract": "Working out the tool arguments",
    "synthesise": "Writing the answer",
    "orchestrate": "Choosing the next tool",
}


class Recorder:
    """The calls made while answering one question."""

    def __init__(self):
        self.calls = []

    def note(self, operation: str, model: str, message, seconds: float,
             detail: str = ""):
        """Record one model call from its response.

        `message` is whatever the client returned — a full message or a merged
        stream chunk. Both carry usage_metadata; a chunk that somehow does not
        is recorded with zeros rather than dropped, so a missing call shows up
        as a gap instead of silently improving the totals.
        """
        usage = getattr(message, "usage_metadata", None) or {}
        details = usage.get("output_token_details") or {}
        self.calls.append({
            "operation": operation,
            "label": OPERATION_LABELS.get(operation, operation),
            "model": model,
            "detail": detail,
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "reasoning_tokens": int(details.get("reasoning") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
            "seconds": round(seconds, 2),
            "measured": bool(usage),
        })

    def summary(self) -> dict:
        """Totals, and a per-operation breakdown ordered by cost."""
        if not self.calls:
            return {}

        by_operation = {}
        for call in self.calls:
            entry = by_operation.setdefault(call["operation"], {
                "operation": call["operation"], "label": call["label"],
                "calls": 0, "input_tokens": 0, "output_tokens": 0,
                "reasoning_tokens": 0, "total_tokens": 0, "seconds": 0.0,
            })
            entry["calls"] += 1
            for field in ("input_tokens", "output_tokens", "reasoning_tokens",
                          "total_tokens"):
                entry[field] += call[field]
            entry["seconds"] = round(entry["seconds"] + call["seconds"], 2)

        total = sum(c["total_tokens"] for c in self.calls)
        breakdown = sorted(by_operation.values(),
                           key=lambda e: -e["total_tokens"])
        for entry in breakdown:
            entry["share_pct"] = round(entry["total_tokens"] / total * 100, 1) if total else 0.0

        unmeasured = [c["operation"] for c in self.calls if not c["measured"]]
        return {
            "calls": self.calls,
            "by_operation": breakdown,
            "model_calls": len(self.calls),
            "input_tokens": sum(c["input_tokens"] for c in self.calls),
            "output_tokens": sum(c["output_tokens"] for c in self.calls),
            "reasoning_tokens": sum(c["reasoning_tokens"] for c in self.calls),
            "total_tokens": total,
            "seconds": round(sum(c["seconds"] for c in self.calls), 2),
            # The cap is per MINUTE, so the useful framing is what share of one
            # minute's allowance this question consumed.
            "minute_budget_pct": round(total / MINUTE_TOKEN_BUDGET * 100, 1),
            "unmeasured_calls": unmeasured,
        }


# Groq's per-minute ceiling on the gpt-oss models. Not a cost estimate — the
# rate limit is what actually stops a third question in a row from running.
MINUTE_TOKEN_BUDGET = 8000


def start() -> Recorder:
    recorder = Recorder()
    _current.set(recorder)
    return recorder


def get() -> Recorder:
    return _current.get()


def note(operation: str, model: str, message, seconds: float, detail: str = ""):
    """Record a call if this request is being measured; otherwise do nothing."""
    recorder = _current.get()
    if recorder is not None:
        recorder.note(operation, model, message, seconds, detail)


@contextlib.contextmanager
def measure(operation: str, model: str, detail: str = ""):
    """Time a model call and record whatever it returned.

    Usage:
        with telemetry.measure("classify", MODEL) as m:
            m.response = client.invoke(messages)

    The response is assigned onto the handle rather than returned, so a caller
    that raises still leaves a timed entry behind — a failed call spent tokens
    too, and hiding it would make the totals look better than the truth.
    """
    class _Handle:
        response = None

    handle = _Handle()
    started = time.time()
    try:
        yield handle
    finally:
        note(operation, model, handle.response, time.time() - started, detail)


def summary() -> dict:
    recorder = _current.get()
    return recorder.summary() if recorder is not None else {}
