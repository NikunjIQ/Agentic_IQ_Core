"""Which metrics a question asks for, and how a reading is rendered.

Zabbix key searches are substring-based, so every metric carries a precise match
test as well as a search term: asking for 'net.if' also returns discard and error
counters, which is why a plain traffic question once answered with "Inbound
packets discarded: 0".
"""

from .common import format_duration, mentions, numeric_key, short_text

# Zabbix does not model equipment lifecycle at all — there is no inventory field
# for it and no item collects it. Saying so, and handing back the model and
# serial that an external lookup would need, beats an empty table.
EOL_NOTICE = (
    "Zabbix does not record end-of-life or end-of-support dates — it has no such "
    "field and collects no such item, so this cannot be answered from monitoring "
    "data. The hardware model and serial number reported above are what an "
    "external lifecycle lookup (e.g. the vendor's EoX data) would need."
)


def requested_metrics(item_keyword: str) -> list:
    """Every metric named in the keyword, not just the first one recognised.

    'cpu & memory utilization' has to come back with both.

    'kind' decides how a reading is rendered: 'numeric' sorts highest-first,
    'duration' converts seconds to days, 'text' keeps the string as collected and
    never sorts — an IOS version has no ordering.
    """
    text = (item_keyword or '').lower()
    metrics = []

    if 'cpu' in text or 'processor' in text:
        metrics.append({
            'label': 'CPU',
            'kind': 'numeric',
            'key_searches': ['cpu'],
            'match': lambda i: 'cpu' in (i.get('key_', '') + i.get('name', '')).lower(),
        })
    if 'memory' in text or mentions(text, 'ram'):
        metrics.append({
            'label': 'Memory',
            'kind': 'numeric',
            'key_searches': ['memory'],
            'match': lambda i: 'memory' in (i.get('key_', '') + i.get('name', '')).lower(),
        })
    if any(w in text for w in ('traffic', 'network', 'bandwidth', 'throughput')):
        metrics.append({
            'label': 'Network traffic (bits in/out)',
            'kind': 'numeric',
            'key_searches': ['net.if'],
            # net.if.in.discards / .errors / .speed / .status also match "net.if",
            # so pin to the exact keys that carry actual throughput.
            'match': lambda i: i.get('key_', '').split('[')[0] in ('net.if.in', 'net.if.out'),
        })
    if any(w in text for w in ('disk', 'space', 'filesystem', 'storage')):
        metrics.append({
            'label': 'Disk space',
            'kind': 'numeric',
            'key_searches': ['vfs.fs'],
            'match': lambda i: i.get('key_', '').startswith('vfs.fs'),
        })

    # Uptime lives under a different key depending on how the device is polled:
    # SNMP network kit reports system.net.uptime[sysUpTime.0], agent hosts report
    # system.uptime. Searching only one answers for half the estate.
    if any(w in text for w in ('uptime', 'up time', 'how long', 'last reboot',
                               'rebooted', 'running since')):
        metrics.append({
            'label': 'Uptime',
            'kind': 'duration',
            'key_searches': ['system.uptime', 'system.net.uptime', 'system.hw.uptime'],
            'match': lambda i: i.get('key_', '').split('[')[0] in (
                'system.uptime', 'system.net.uptime', 'system.hw.uptime'),
        })

    # "Which agent version" is a different item from "which OS version", so it is
    # resolved first and suppresses the generic 'version' trigger below.
    wants_agent_version = 'agent' in text and 'version' in text
    if wants_agent_version:
        metrics.append({
            'label': 'Zabbix agent version',
            'kind': 'text',
            'key_searches': ['agent.version'],
            'match': lambda i: i.get('key_', '').split('[')[0] == 'agent.version',
        })

    if (mentions(text, 'ios', 'os')
            or any(w in text for w in ('operating system', 'software', 'firmware'))
            or ('version' in text and not wants_agent_version)):
        metrics.append({
            'label': 'OS / software version',
            'kind': 'text',
            'key_searches': ['system.sw.os', 'system.descr'],
            'match': lambda i: i.get('key_', '').split('[')[0] in (
                'system.sw.os', 'system.descr'),
        })

    wants_eol = any(w in text for w in ('end of life', 'end-of-life', 'end of support',
                                        'end-of-support', 'warranty', 'expiry',
                                        'expiration', 'decommission')) \
        or mentions(text, 'eol', 'eos')

    if wants_eol or any(w in text for w in ('model', 'serial', 'hardware', 'chassis')):
        metrics.append({
            'label': 'Hardware (model / serial)',
            'kind': 'text',
            'key_searches': ['system.hw'],
            # system.hw.uptime is uptime, not an identity field.
            'match': lambda i: (i.get('key_', '').startswith('system.hw')
                                and not i.get('key_', '').startswith('system.hw.uptime')),
        })

    if wants_eol:
        # Placed last so the model/serial it points at have already been listed.
        metrics.append({
            'label': 'End-of-life / end-of-support',
            'kind': 'text',
            'key_searches': [],
            'unavailable': EOL_NOTICE,
            'match': lambda i: False,
        })

    if mentions(text, 'ping', 'icmp', 'latency') or any(
            w in text for w in ('packet loss', 'response time', 'reachability')):
        metrics.append({
            'label': 'ICMP ping / latency / loss',
            'kind': 'numeric',
            'key_searches': ['icmpping'],
            'match': lambda i: i.get('key_', '').split('[')[0].startswith('icmpping'),
        })

    return metrics


def render_metric(metric: dict, matched: list) -> tuple:
    """One metric's readings as (summary text, rows kept for the table).

    An item that exists but carries nothing is reported as not collected rather
    than as a value. Zabbix marks a check it could not perform as unsupported
    (state=1), and an unreachable agent leaves the value empty — both are real
    findings, and both read as "0" or "" if passed through unexamined.
    """
    kind = metric.get('kind', 'numeric')
    label = metric['label']

    unsupported = [i for i in matched if str(i.get('state')) == '1']

    def collected(item):
        raw = str(item.get('lastvalue', '')).strip()
        if not raw:
            return False
        # Uptime and ping counters read 0 when nothing came back at all.
        return not (kind == 'duration' and format_duration(raw) is None)

    live = [i for i in matched if collected(i)]

    if not live:
        note = f"{label}: no reading collected"
        if unsupported:
            note += f" ({len(unsupported)} item(s) unsupported on this host)"
        elif matched:
            note += (f" — {len(matched)} item(s) are configured but hold no value, "
                     f"which usually means the agent or SNMP poll is not responding")
        else:
            note += " — no such item is configured on this host"
        return note + ".", []

    if kind == 'duration':
        live.sort(key=lambda i: numeric_key(i.get('lastvalue')), reverse=True)
        shown = [f"{i['name']}: {format_duration(i.get('lastvalue'))} "
                 f"({i.get('lastvalue')} seconds)" for i in live[:8]]
    elif kind == 'text':
        # Version strings have no ordering; keep collection order.
        shown = [f"{i['name']}: {short_text(i.get('lastvalue'))}" for i in live[:8]]
    else:
        live.sort(key=lambda i: numeric_key(i.get('lastvalue')), reverse=True)
        shown = [f"{i['name']}: {i.get('lastvalue')} {i.get('units', '')}".strip()
                 for i in live[:8]]

    text = f"{label} ({len(live)} reading(s)"
    if kind == 'numeric':
        text += ", highest first"
    text += "): " + "; ".join(shown)
    if unsupported:
        text += f" [{len(unsupported)} further item(s) unsupported on this host]"

    rows = [{**i, 'metric': label,
             'display_value': (format_duration(i.get('lastvalue')) if kind == 'duration'
                               else short_text(i.get('lastvalue')))}
            for i in live]
    return text + ".", rows
