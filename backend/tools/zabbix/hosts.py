"""What exists: hosts and host groups."""

import pandas as pd

from .common import (HOST_AVAILABILITY_PARAMS, OFFLINE_STATUSES,
                     derive_availability, parse_status_filter)


def query_hosts(rpc, host_filter=None, status_filter=None, group_filter=None) -> dict:
    hosts = rpc("host.get", HOST_AVAILABILITY_PARAMS)

    # "How many devices run Zabbix Agent 2" is an estate-wide question, so the
    # version has to be on the host rows rather than reachable only by asking
    # about one device at a time.
    agent_versions = {}
    try:
        for item in rpc("item.get", {
                "output": ["hostid", "key_", "lastvalue"],
                "hostids": [h["hostid"] for h in hosts],
                "search": {"key_": "agent.version"}}):
            value = str(item.get("lastvalue", "")).strip()
            if value:
                agent_versions[str(item["hostid"])] = value
    except Exception:
        pass   # the host counts still answer the question without it

    for h in hosts:
        h['agent_version'] = agent_versions.get(str(h['hostid']), '')
        h['available_str'] = derive_availability(h)

        types = [str(i.get('type')) for i in h.get('interfaces', [])]
        h['is_zabbix_agent'] = '1' in types
        h['is_snmp'] = '2' in types

        groups = [g.get('name', '').lower() for g in h.get('hostgroups', [])]
        h['is_router'] = any('router' in g for g in groups)
        h['is_switch'] = any('switch' in g for g in groups)
        h['group_names'] = ", ".join(g.get('name', '') for g in h.get('hostgroups', []))

    df = pd.DataFrame(hosts)
    summary = "No hosts found matching criteria."

    if not df.empty:
        if host_filter:
            df = df[df['host'].str.contains(host_filter, case=False, na=False)
                    | df['name'].str.contains(host_filter, case=False, na=False)]
        if group_filter:
            g_lower = group_filter.lower()
            if 'router' in g_lower:
                df = df[df['is_router'] == True]
            elif 'switch' in g_lower:
                df = df[df['is_switch'] == True]
            else:
                df = df[df['group_names'].str.contains(g_lower, case=False, na=False)]
        if status_filter:
            wanted = parse_status_filter(status_filter)
            if wanted:
                df = df[df['available_str'].isin(wanted)]

        total = len(df)
        up = int((df['available_str'] == 'up').sum())
        unknown = int((df['available_str'] == 'unknown').sum())
        offline_mask = df['available_str'].isin(OFFLINE_STATUSES)
        offline = int(offline_mask.sum())
        snmp = int(df['is_snmp'].sum()) if 'is_snmp' in df.columns else 0
        agent = int(df['is_zabbix_agent'].sum()) if 'is_zabbix_agent' in df.columns else 0

        summary = (
            f"Total hosts matching criteria: {total}. "
            f"Up: {up}. Down (not responding): {offline}. Unknown/not yet checked: {unknown}. "
            f"SNMP-monitored devices: {snmp}. Zabbix-agent-monitored devices: {agent}."
        )
        if offline:
            offline_hosts = df[offline_mask]['name'].tolist()
            summary += f" Down hosts: {', '.join(offline_hosts)}."
        if 'is_router' in df.columns:
            summary += (f" Of these, {int(df['is_router'].sum())} are routers and "
                        f"{int(df['is_switch'].sum())} are switches.")
        if 'agent_version' in df.columns and agent:
            reported = df[df['agent_version'].astype(bool)]
            if not reported.empty:
                by_version = reported['agent_version'].value_counts()
                summary += (" Zabbix agent versions reported: " + ", ".join(
                    f"{version} on {count} host(s)"
                    for version, count in by_version.items()) + ".")
            silent = agent - len(reported)
            if silent > 0:
                # Two different causes, and the tool cannot tell them apart
                # without another call: the agent is down, or no agent.version
                # item was ever configured on that host.
                summary += (f" {silent} agent-monitored host(s) report no version "
                            f"— either the agent is not responding or no "
                            f"agent.version item is configured on them.")
            # agent.version gives the version but not the variant, and no host
            # collects agent.variant — so the Agent 1 vs Agent 2 split genuinely
            # cannot be answered from this Zabbix.
            summary += (" Zabbix does not distinguish Zabbix Agent 1 from "
                        "Zabbix Agent 2 here: that requires the 'agent.variant' "
                        "item, which is not collected on any host. Only the "
                        "agent version above is available.")
        if total <= 25:
            listing = ", ".join(f"{r['name']} ({r['available_str']})"
                                for _, r in df.iterrows())
            summary += f" Hosts: {listing}."

    return {"summary": summary, "dataframe": df,
            "raw": {"hosts": df.to_dict('records') if not df.empty else []}}


def query_hostgroups(rpc) -> dict:
    groups = rpc("hostgroup.get", {"output": ["groupid", "name"]})
    hosts = rpc("host.get", HOST_AVAILABILITY_PARAMS)

    group_stats = {g['groupid']: {'total': 0, 'down_names': []} for g in groups}
    for h in hosts:
        is_offline = derive_availability(h) in OFFLINE_STATUSES
        host_name = h.get('name') or h.get('host', '')
        for g in h.get('hostgroups', []):
            stats = group_stats.get(g.get('groupid'))
            if stats is None:
                continue
            stats['total'] += 1
            if is_offline:
                stats['down_names'].append(host_name)

    for g in groups:
        stats = group_stats[g['groupid']]
        g['total_devices'] = stats['total']
        g['down_devices'] = len(stats['down_names'])
        g['down_device_names'] = ", ".join(stats['down_names'])

    df = pd.DataFrame(groups)
    total_down = sum(len(s['down_names']) for s in group_stats.values())
    summary = f"Total host groups configured: {len(groups)}."
    if not df.empty:
        df = df.sort_values('down_devices', ascending=False)
        highest = df.iloc[0]
        if highest['down_devices'] > 0:
            summary += (
                f" Group '{highest['name']}' has the highest number of down devices "
                f"({highest['down_devices']} of {highest['total_devices']} devices in the group)."
                f" Down devices in that group: {highest['down_device_names']}."
            )
        else:
            summary += " No host group currently has any down devices."

    return {"summary": summary, "dataframe": df,
            "raw": {"groups": groups, "total_down_devices": total_down}}
