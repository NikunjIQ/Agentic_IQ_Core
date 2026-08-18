"""A live view of the graph while a question runs.

The backend already streams what it is doing; until now that arrived as a single
line of prose. The same events carry the node's identity, so the graph can be
drawn and lit as the run moves through it — which is the only way to see that a
question took the cheap route rather than the expensive one.

Drawn as inline SVG rather than a chart library: the graph is seven fixed nodes,
so the layout is known and hand-authoring it costs nothing at runtime.
"""

# Layout is fixed because the graph is. x/y are the top-left of each box in a
# 640x300 viewBox; the edges below are drawn against the same coordinates.
NODES = {
    "classify":     {"x": 232, "y": 18,  "w": 176, "h": 42, "label": "classify"},
    "clarify":      {"x": 16,  "y": 108, "w": 176, "h": 42, "label": "clarify"},
    "simple_path":  {"x": 232, "y": 108, "w": 176, "h": 42, "label": "simple_path"},
    "complex_path": {"x": 448, "y": 108, "w": 176, "h": 42, "label": "complex_path"},
    "respond":      {"x": 232, "y": 198, "w": 176, "h": 42, "label": "respond"},
}

# (path, which node must be active/done for the edge to count as travelled)
EDGES = [
    ("M320,60 L320,108", "clarify",      "M320,74 L104,74 L104,108"),
    ("M320,60 L320,108", "simple_path",  "M320,60 L320,108"),
    ("M320,60 L320,108", "complex_path", "M320,74 L536,74 L536,108"),
    ("M104,150 L104,219 L232,219", "clarify_out", None),
    ("M320,150 L320,198", "simple_out", None),
    ("M536,150 L536,219 L408,219", "complex_out", None),
]

_STATE_STYLE = {
    "idle":   ("#cbd5dc", "#8b98a5", "none"),
    "active": ("#1b6e6b", "#1b6e6b", "#e2efee"),
    "done":   ("#5c9a97", "#3f6f6d", "none"),
}


def render(states: dict, current_tool: str = "", note: str = "") -> str:
    """SVG for the graph with each node in state idle | active | done.

    `states` maps node name to one of those three. Anything not mentioned is
    idle, so a run that never reaches complex_path simply leaves it grey.
    """
    parts = [
        '<svg viewBox="0 0 640 300" width="100%" '
        'style="max-width:640px;height:auto;font-family:ui-monospace,SFMono-Regular,Menlo,monospace" '
        'role="img" aria-label="Live view of the agent graph">',
        '<defs><marker id="lgar" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
        '<path d="M0,0 L10,5 L0,10 z" fill="#8b98a5"/></marker></defs>',
    ]

    def travelled(node):
        return states.get(node) in ("active", "done")

    branch_paths = {
        "clarify": "M320,74 L104,74 L104,108",
        "simple_path": "M320,60 L320,108",
        "complex_path": "M320,74 L536,74 L536,108",
    }
    out_paths = {
        "clarify": "M104,150 L104,219 L232,219",
        "simple_path": "M320,150 L320,198",
        "complex_path": "M536,150 L536,219 L408,219",
    }

    for branch, d in branch_paths.items():
        lit = travelled(branch)
        colour = "#1b6e6b" if lit else "#cbd5dc"
        width = 2.4 if lit else 1.4
        dash = "" if lit else ' stroke-dasharray="5 4"'
        parts.append(f'<path d="{d}" fill="none" stroke="{colour}" '
                     f'stroke-width="{width}"{dash} marker-end="url(#lgar)"/>')
        if lit:
            parts.append(f'<path d="{out_paths[branch]}" fill="none" stroke="{colour}" '
                         f'stroke-width="{width}" marker-end="url(#lgar)"/>')

    for name, box in NODES.items():
        state = states.get(name, "idle")
        stroke, text, fill = _STATE_STYLE[state]
        fill_attr = fill if fill != "none" else "#ffffff00"
        parts.append(
            f'<rect x="{box["x"]}" y="{box["y"]}" width="{box["w"]}" height="{box["h"]}" '
            f'rx="8" fill="{fill_attr}" stroke="{stroke}" '
            f'stroke-width="{2.4 if state == "active" else 1.4}"/>')
        parts.append(
            f'<text x="{box["x"] + box["w"] / 2}" y="{box["y"] + 26}" text-anchor="middle" '
            f'font-size="13" fill="{text}">{box["label"]}</text>')
        if state == "active":
            # A quiet pulse marks where the run is right now. Reduced-motion users
            # still get the colour and the ring, so nothing depends on animation.
            parts.append(
                f'<circle cx="{box["x"] + box["w"] - 14}" cy="{box["y"] + 14}" r="4" '
                f'fill="#1b6e6b"><animate attributeName="opacity" values="1;.25;1" '
                f'dur="1.1s" repeatCount="indefinite"/></circle>')

    caption = current_tool or note
    if caption:
        parts.append(f'<text x="320" y="278" text-anchor="middle" font-size="12" '
                     f'fill="#657483">{caption}</text>')

    parts.append("</svg>")
    return "".join(parts)


def initial() -> dict:
    return {}
