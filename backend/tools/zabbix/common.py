"""Shared vocabulary for the Zabbix branches.

Availability, status words and value formatting live here because more than one
branch depends on them agreeing. When the host counts and the host-group counts
derived "down" differently, the same estate answered 0 and 2 to the same
question.
"""

import re
from typing import Optional

# Zabbix 6.0+ removed the host-level "available" field. Passive availability now
# lives on each interface, and active-agent availability on the host's
# "active_available". Every branch that needs availability must request the same
# fields and derive status the same way, so both live here.
HOST_AVAILABILITY_PARAMS = {
    "output": ["hostid", "host", "name", "status", "active_available"],
    "selectInterfaces": ["type", "details", "available", "error"],
    "selectHostGroups": ["name", "groupid"]
}

# A host counts as "down" for reporting purposes if it is not answering, whether or
# not Zabbix attached a reason. An interface that goes unavailable almost always
# carries an error string, so counting only 'down' would report zero forever.
OFFLINE_STATUSES = ('down', 'unreachable')

# What a user means by each status word. "Down" has to cover 'unreachable' or the
# tool contradicts itself: the host-group counts treat a non-responding host as
# down, so asking "how many devices are down" must not answer 0 while asking
# "which group has the most down devices" answers 2.
STATUS_ALIASES = {
    'unreachable': ('unreachable',),
    'unknown': ('unknown',),
    'offline': OFFLINE_STATUSES,
    'down': OFFLINE_STATUSES,
    'up': ('up',),
}


def parse_status_filter(status_filter: str) -> set:
    """Turn 'down', 'unreachable or unknown', 'up,down' into a set of statuses.

    Each status word is matched independently so multi-status questions
    ("list hosts that are unreachable or unknown") do not silently return only
    the first one that matched.
    """
    text = (status_filter or '').lower()
    wanted = set()
    for keyword, statuses in STATUS_ALIASES.items():
        if keyword in text:
            wanted.update(statuses)
    return wanted


def derive_availability(host: dict) -> str:
    """Map a Zabbix 6.0+ host object to 'up', 'down', 'unreachable' or 'unknown'.

    Expects the host to have been fetched with HOST_AVAILABILITY_PARAMS.
    """
    interfaces = host.get('interfaces', [])
    avail_set = set(str(i.get('available', '0')) for i in interfaces)
    active_avail = str(host.get('active_available', '0'))

    if '2' in avail_set or active_avail == '2':
        status = 'down'
    elif '1' in avail_set or active_avail == '1':
        status = 'up'
    else:
        status = 'unknown'

    # An interface that failed carries the reason it failed; surface that as
    # 'unreachable' so callers can tell "no response" from "reported down".
    if any(i.get('error') for i in interfaces):
        status = 'unreachable'

    return status


def numeric_key(value):
    """Sort key for item values; non-numeric readings sink to the bottom."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return float('-inf')


def mentions(text: str, *words) -> bool:
    """Whole-word test, for keywords that are substrings of unrelated words.

    'os' sits inside 'hosts' and 'ram' inside 'program', so the substring tests
    used for unambiguous keywords ('cpu', 'memory') would fire on questions that
    never asked for those metrics.
    """
    return any(re.search(rf'\b{re.escape(w)}\b', text) for w in words)


def format_duration(seconds) -> Optional[str]:
    """Seconds to '11d 5h'. None when the reading is absent or zero.

    A device that is running always has a positive uptime, so 0 means the item
    exists but nothing was collected — which must not be reported as "up for 0
    seconds".
    """
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:
        parts.append(f"{minutes}m")
    return " ".join(parts) or f"{total}s"


def short_text(value) -> str:
    """A collected string, flattened and capped.

    Cisco's sysDescr is a multi-line block with a support URL and a copyright
    notice. Passed through whole it crowds out every other metric in the summary
    the model reads, so the reading is kept but trimmed.
    """
    flat = " ".join(str(value).split())
    return flat if len(flat) <= 140 else flat[:137] + "..."
