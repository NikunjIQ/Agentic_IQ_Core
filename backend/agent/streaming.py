"""Progress and token streaming for a graph run.

The graph is synchronous and multi-step: classify, call tools, then write an
answer. Only the last step produces user-facing text, and the steps before it
are where the seconds go — so the useful thing to stream is BOTH: status while
the tools run, then tokens as the answer is written.

Rather than duplicate the orchestration in a streaming variant (which would
drift from the real one), the graph runs unchanged in a worker thread and the
nodes publish events here. A context variable carries the emitter, set inside
that thread so the nodes can find it without threading it through the state.
"""

import contextlib
import contextvars
import queue

_current = contextvars.ContextVar("iqcore_emitter", default=None)

DONE = object()


class Emitter:
    """A queue of events for one request. Not shared between requests."""

    def __init__(self):
        self.events = queue.Queue()

    def status(self, message: str):
        self.events.put({"type": "status", "message": message})

    def node(self, name: str, phase: str):
        self.events.put({"type": "node", "node": name, "phase": phase})

    def tool(self, name: str):
        self.events.put({"type": "tool", "tool": name})

    def token(self, text: str):
        if text:
            self.events.put({"type": "token", "text": text})

    def finish(self, payload: dict):
        self.events.put({"type": "done", "payload": payload})
        self.events.put(DONE)

    def fail(self, message: str):
        self.events.put({"type": "error", "message": message})
        self.events.put(DONE)


def set_emitter(emitter):
    _current.set(emitter)


def get_emitter():
    return _current.get()


def status(message: str):
    """Report progress if this run is being streamed; otherwise do nothing."""
    emitter = _current.get()
    if emitter is not None:
        emitter.status(message)


def tool_started(name: str):
    """Name the tool now running, for callers that draw the run."""
    emitter = _current.get()
    if emitter is not None:
        emitter.tool(name)


@contextlib.contextmanager
def node(name: str):
    """Mark a graph node as entered and left.

    The status messages are written for a person to read; these carry the node's
    identity instead, so a caller can light up the graph as it executes. The exit
    event is emitted from a finally block, because a node that raises has still
    stopped running and the drawing must not leave it spinning forever.
    """
    emitter = _current.get()
    if emitter is None:
        yield
        return
    emitter.node(name, "enter")
    try:
        yield
    finally:
        emitter.node(name, "exit")


def stream_text(client, messages):
    """Yield an answer token by token when streaming, in one call when not.

    Returns the complete text either way, so callers do not need two code paths.
    """
    emitter = _current.get()
    if emitter is None:
        return client.invoke(messages).content

    parts = []
    for chunk in client.stream(messages):
        piece = getattr(chunk, "content", "") or ""
        if piece:
            parts.append(piece)
            emitter.token(piece)
    return "".join(parts)


def stream_with_tools(client, messages):
    """Stream a call that may return either tool calls or a final answer.

    The model decides mid-generation which it is doing, so chunks are summed
    back into one message to recover tool_calls, while any prose is emitted
    live. When the model chooses a tool, nothing is shown — correctly, since
    there is no answer text yet.
    """
    emitter = _current.get()
    if emitter is None:
        return client.invoke(messages)

    merged = None
    for chunk in client.stream(messages):
        merged = chunk if merged is None else merged + chunk
        piece = getattr(chunk, "content", "") or ""
        if piece:
            emitter.token(piece)
    return merged
