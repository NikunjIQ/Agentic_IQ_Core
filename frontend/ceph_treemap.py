"""A treemap of the Ceph CRUSH hierarchy.

`query_ceph` with query_type='crush' returns root -> host -> OSD rows; this draws
them so the shape of the cluster is visible at a glance: how capacity is split
between hosts, and how lopsided that split is.

Slice-and-dice rather than squarified: the hierarchy is two levels deep over a
handful of nodes, so hosts become columns whose width is proportional to their
capacity and OSDs become rows inside them. Area stays honest — an OSD twice the
size draws twice the area — and the layout is stable, which a squarified
treemap is not when a weight changes slightly.

Hand-authored SVG rather than a charting library: it is two nested loops, and
the frontend has no plotting dependency.
"""

WIDTH = 720
HEIGHT = 340
PAD = 3
HEADER = 22

# Reads on Streamlit's light and dark themes alike.
_UP = "#1b6e6b"
_UP_TEXT = "#ffffff"
_DOWN = "#a93b2e"
_EDGE = "#e7ecef"
_LABEL = "#5c6773"


def _esc(text) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def render(rows) -> str:
    """SVG for a list of dicts with host, osd, size_GB, device_class and up.

    Returns an empty string when there is nothing to draw, so the caller can
    simply skip rendering rather than showing an empty frame.
    """
    rows = [r for r in (rows or []) if (r.get("size_GB") or 0) > 0]
    if not rows:
        return ""

    by_host = {}
    for row in rows:
        by_host.setdefault(row.get("host", "?"), []).append(row)

    total = sum(r.get("size_GB", 0) for r in rows)
    if total <= 0:
        return ""

    parts = [
        f'<svg viewBox="0 0 {WIDTH} {HEIGHT}" width="100%" '
        f'style="max-width:{WIDTH}px;height:auto;'
        f'font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif" '
        f'role="img" aria-label="Treemap of the Ceph CRUSH hierarchy: '
        f'{len(by_host)} hosts and {len(rows)} OSDs sized by capacity.">'
    ]

    x = 0.0
    for host in sorted(by_host):
        osds = sorted(by_host[host], key=lambda r: -(r.get("size_GB") or 0))
        host_total = sum(r.get("size_GB", 0) for r in osds)
        col_w = (host_total / total) * WIDTH

        parts.append(
            f'<text x="{x + col_w / 2:.1f}" y="14" text-anchor="middle" '
            f'font-size="12" font-weight="600" fill="{_LABEL}">'
            f'{_esc(host)} · {host_total:,.0f} GB</text>')

        y = HEADER
        usable_h = HEIGHT - HEADER
        for osd in osds:
            size = osd.get("size_GB") or 0
            row_h = (size / host_total) * usable_h if host_total else 0
            up = bool(osd.get("up", True))
            fill = _UP if up else _DOWN

            parts.append(
                f'<rect x="{x + PAD:.1f}" y="{y + PAD:.1f}" '
                f'width="{max(col_w - 2 * PAD, 1):.1f}" '
                f'height="{max(row_h - 2 * PAD, 1):.1f}" rx="4" '
                f'fill="{fill}" stroke="{_EDGE}" stroke-width="1"/>')

            # Only label a rectangle tall enough to hold the text.
            if row_h > 34:
                cx = x + col_w / 2
                parts.append(
                    f'<text x="{cx:.1f}" y="{y + row_h / 2 - 3:.1f}" '
                    f'text-anchor="middle" font-size="12" font-weight="600" '
                    f'fill="{_UP_TEXT}">{_esc(osd.get("osd"))}</text>')
                parts.append(
                    f'<text x="{cx:.1f}" y="{y + row_h / 2 + 13:.1f}" '
                    f'text-anchor="middle" font-size="10.5" fill="{_UP_TEXT}" '
                    f'opacity="0.85">{size:,.0f} GB · '
                    f'{_esc(str(osd.get("device_class", "")).upper())}'
                    f'{"" if up else " · DOWN"}</text>')
            elif row_h > 14:
                parts.append(
                    f'<text x="{x + col_w / 2:.1f}" y="{y + row_h / 2 + 4:.1f}" '
                    f'text-anchor="middle" font-size="10" fill="{_UP_TEXT}">'
                    f'{_esc(osd.get("osd"))}</text>')
            y += row_h
        x += col_w

    parts.append("</svg>")
    return "".join(parts)


def caption(rows) -> str:
    """One line stating what the picture shows, including anything not drawn."""
    rows = list(rows or [])
    drawn = [r for r in rows if (r.get("size_GB") or 0) > 0]
    hosts = {r.get("host") for r in drawn}
    down = [r for r in drawn if not r.get("up", True)]
    classes = {str(r.get("device_class", "")).upper() for r in drawn if r.get("device_class")}

    text = (f"Area is proportional to raw capacity. {len(drawn)} OSD(s) across "
            f"{len(hosts)} host(s), {sum(r.get('size_GB', 0) for r in drawn):,.0f} GB total"
            f"{' · ' + ', '.join(sorted(classes)) if classes else ''}.")
    if down:
        text += f" {len(down)} OSD(s) not up, shown in red."
    if len(rows) != len(drawn):
        text += f" {len(rows) - len(drawn)} OSD(s) reported no capacity and are not drawn."
    return text
