import re
from typing import Optional

import pandas as pd
from langchain_core.tools import tool

from tools.clients import PrometheusClient

# Ready-made PromQL for the things people ask for by name. Each entry is
# (label, template, unit). {sel} becomes an instance selector, {w} a rate window.
# The alternative is making the model author PromQL from memory against 2453
# metric names, which is where hallucinated metric names come from.
_INTENTS = {
    "cpu": ("CPU used %",
            '100 - (avg by (instance) (rate(node_cpu_seconds_total{{mode="idle"{sel}}}[{w}])) * 100)',
            "%"),
    "memory": ("Memory used %",
               '100 - (100 * node_memory_MemAvailable_bytes{{{selb}}} '
               '/ node_memory_MemTotal_bytes{{{selb}}})',
               "%"),
    "disk": ("Disk used %",
             '100 - (100 * node_filesystem_avail_bytes{{fstype!~"tmpfs|devtmpfs|overlay|squashfs|iso9660"{sel}}} '
             '/ node_filesystem_size_bytes{{fstype!~"tmpfs|devtmpfs|overlay|squashfs|iso9660"{sel}}})',
             "%"),
    "load": ("Load average (1m)", 'node_load1{{{selb}}}', ""),
    "uptime": ("Uptime (days)", '(time() - node_boot_time_seconds{{{selb}}}) / 86400', "days"),
    "network_in": ("Network received",
                   'rate(node_network_receive_bytes_total{{device!~"lo|veth.*"{sel}}}[{w}]) * 8', "bps"),
    "network_out": ("Network transmitted",
                    'rate(node_network_transmit_bytes_total{{device!~"lo|veth.*"{sel}}}[{w}]) * 8', "bps"),
    "up": ("Target up (1) / down (0)", 'up{{{selb}}}', ""),
}

_INTENT_ALIASES = {
    "processor": "cpu", "cpu_usage": "cpu", "cpu utilization": "cpu",
    "ram": "memory", "mem": "memory", "memory_usage": "memory",
    "filesystem": "disk", "storage": "disk", "disk_usage": "disk", "space": "disk",
    "loadavg": "load", "network": "network_in", "traffic": "network_in",
    "bandwidth": "network_in", "availability": "up", "status": "up",
}


def _resolve_intent(metric: Optional[str]) -> Optional[str]:
    if not metric:
        return None
    key = metric.strip().lower().replace("-", "_")
    if key in _INTENTS:
        return key
    if key in _INTENT_ALIASES:
        return _INTENT_ALIASES[key]
    for alias, target in _INTENT_ALIASES.items():
        if alias in key:
            return target
    for name in _INTENTS:
        if name in key:
            return name
    return None


# Prometheus exposes no guest-OS label — `os_type` is the virtualisation type
# ("hvm") for every VM. These keywords are the only signal available, so any
# OS split is an inference from naming and must be reported as one.
_WINDOWS_HINTS = ("windows", "win2", "win-", "wins", "msserver", "win_")
_LINUX_HINTS = ("ubuntu", "jammy", "centos", "debian", "linux", "rhel", "alma",
                "rocky", "fedora", "suse", "eve-ng", "eve")


def _guess_os(*texts) -> str:
    blob = " ".join(str(t or "").lower() for t in texts)
    if any(h in blob for h in _WINDOWS_HINTS):
        return "Windows (inferred)"
    if any(h in blob for h in _LINUX_HINTS):
        return "Linux (inferred)"
    return "unknown"


def _vm_inventory(client) -> list:
    """One row per VM, joining libvirt runtime state to OpenStack identity.

    libvirt knows a VM as 'instance-00000015'; OpenStack knows it as 'jammy'.
    libvirt_domain_openstack_info carries both, so it is the join key — without
    it every VM answer would be unreadable domain ids.
    """
    identity = {}
    for series in client.instant("libvirt_domain_openstack_info"):
        labels = series.get("metric", {})
        domain = labels.get("domain")
        if domain:
            identity[domain] = {
                "vm": labels.get("instance_name") or domain,
                "flavor": labels.get("flavor_name", ""),
                "project": labels.get("project_name", ""),
            }

    # OpenStack's own view adds status and which hypervisor it landed on.
    nova = {}
    for series in client.instant("openstack_nova_server_status"):
        labels = series.get("metric", {})
        key = labels.get("instance_libvirt")
        if key:
            nova[key] = {
                "status": labels.get("status", ""),
                "hypervisor": labels.get("hypervisor_hostname", ""),
                "server_id": labels.get("id", ""),
            }

    # Attached volume names are a second OS hint ("windows_vol").
    volumes_by_server = {}
    for series in client.instant("openstack_cinder_volume_status"):
        labels = series.get("metric", {})
        server = labels.get("server_id")
        if server:
            volumes_by_server.setdefault(server, []).append({
                "name": labels.get("name", "(unnamed)"),
                "size_gb": labels.get("size", ""),
                "status": labels.get("status", ""),
            })

    rows = []
    for series in client.instant("libvirt_domain_info_state"):
        labels = series.get("metric", {})
        domain = labels.get("domain", "")
        try:
            state = int(float(series["value"][1]))
        except (KeyError, ValueError, TypeError):
            state = -1

        ident = identity.get(domain, {"vm": domain, "flavor": "", "project": ""})
        extra = nova.get(domain, {})
        attached = volumes_by_server.get(extra.get("server_id", ""), [])

        rows.append({
            "vm": ident["vm"],
            "domain": domain,
            "running": state == 1,
            "state": labels.get("state_desc", ""),
            "status": extra.get("status", ""),
            "hypervisor": extra.get("hypervisor", ""),
            "flavor": ident["flavor"],
            "project": ident["project"],
            "os_guess": _guess_os(ident["vm"], *[v["name"] for v in attached]),
            "volumes": ", ".join(f"{v['name']} ({v['size_gb']}GB, {v['status']})" for v in attached),
        })
    return sorted(rows, key=lambda r: r["vm"])


def _safe_label_value(value: str) -> str:
    """Strip anything that could break out of a PromQL string literal.

    re.escape is wrong here: it produces `192\\.168\\.1\\.30`, and `\\.` is not a
    valid escape inside a PromQL double-quoted string, so Prometheus answers 400.
    Unescaped dots are fine — as a regex `.` still matches the literal dot.
    """
    return re.sub(r'[^A-Za-z0-9._:/\-]', '', str(value))


def _instance_selector(host_filter: Optional[str], job_filter: Optional[str],
                       node_map: dict) -> tuple:
    """Build the PromQL label selector for a host and/or job.

    Returns (with_leading_comma, bare) because some templates already have a
    label inside the braces and some have empty braces.
    """
    clauses = []
    if host_filter:
        needle = host_filter.strip()
        # "server02" -> the address node_exporter reports it under
        address = node_map.get(needle)
        if not address:
            for name, addr in node_map.items():
                if needle.lower() in name.lower():
                    address = addr
                    break
        target = _safe_label_value((address or needle).split(":")[0])
        if target:
            clauses.append(f'instance=~"{target}.*"')
    if job_filter:
        job = _safe_label_value(job_filter.strip())
        if job:
            clauses.append(f'job=~"{job}.*"')

    bare = ",".join(clauses)
    return (("," + bare) if bare else ""), bare


def _rows(series: list, unit: str, node_map: dict) -> list:
    reverse = {addr.split(":")[0]: name for name, addr in node_map.items()}
    out = []
    for item in series:
        labels = dict(item.get("metric", {}))
        try:
            value = float(item["value"][1])
        except (KeyError, ValueError, TypeError):
            continue
        instance = labels.get("instance", "")
        job = labels.get("job", "")
        host = reverse.get(instance.split(":")[0], instance) or job or "(aggregate)"

        # One host runs many exporters and many disks, so the host name alone is
        # not a unique label — without this, five rows all read "server01".
        detail = (labels.get("mountpoint") or labels.get("device")
                  or labels.get("mode") or (job if job and job != host else ""))

        row = {
            "series": f"{host} [{detail}]" if detail else host,
            "host": host,
            "instance": instance,
            "job": job,
            "value": round(value, 2),
            "unit": unit,
        }
        for extra in ("device", "mountpoint", "cpu", "mode", "nodename"):
            if extra in labels:
                row[extra] = labels[extra]
        out.append(row)
    return sorted(out, key=lambda r: r["value"], reverse=True)


@tool
def query_prometheus(
    question: Optional[str] = None,
    metric: Optional[str] = None,
    promql: Optional[str] = None,
    host_filter: Optional[str] = None,
    job_filter: Optional[str] = None,
    search: Optional[str] = None,
    range_window: Optional[str] = None,
) -> dict:
    """
    Query the Prometheus monitoring system for infrastructure metrics on the Linux
    server cluster (OpenStack). Use for CPU, memory, disk, load, uptime, network
    throughput, scrape target health, exporter status, and any custom PromQL.

    This covers DIFFERENT infrastructure from Zabbix. Prometheus monitors the Linux
    servers and their services (MySQL, RabbitMQ, Elasticsearch, HAProxy, memcached,
    ProxySQL, cAdvisor containers, OpenStack, fluentd, blackbox HTTP probes).
    Zabbix monitors the network devices (routers, switches) and Windows hosts.

    Pick ONE of these three modes:
    - metric: a named shortcut. Correct PromQL is supplied for you. Prefer this.
      HOST metrics (the physical servers): 'cpu', 'memory', 'disk', 'load', 'uptime',
      'network_in', 'network_out', 'up', 'targets', 'alerts'.
      VIRTUAL MACHINE / OpenStack shortcuts:
        'vms'            — VM inventory: names, running or not, hypervisor, flavor, OS guess
        'vm_utilization' — CPU and memory per VM, ranked (use for "which VM is busiest")
        'vm_new'         — VMs that appeared recently (set range_window='7d' for 7 days)
        'routers'        — OpenStack Neutron VIRTUAL routers (NOT physical network routers)
        'networks'       — how many VMs sit on private vs public networks
        'volumes'        — Cinder volumes, snapshot count, backup space used
        'containers'     — cAdvisor container series
    - promql: a raw PromQL expression, for anything the shortcuts do not cover.
      Use `search` first to confirm the metric name exists.
    - search: a keyword to find metric names (e.g. 'mysql_slow', 'rabbitmq_queue').
      Returns matching names only, no values. Use this to discover what is available
      before writing promql.

    Optional narrowing:
    - host_filter: a node name ('server01', 'server02', 'server03') or an IP.
    - job_filter: an exporter job ('node', 'mysqld', 'rabbitmq', 'elasticsearch_exporter',
      'haproxy', 'cadvisor', 'memcached', 'proxysql', 'libvirt_exporter', 'fluentd',
      'blackbox_exporter', 'openstack_exporter', 'alertmanager', 'prometheus').
    - range_window: averaging window for rate-based metrics, default '5m'.

    Special values for `metric`: 'targets' lists every scrape target and its health;
    'alerts' lists firing alerts.
    """
    client = PrometheusClient()
    window = (range_window or "5m").strip()
    if not re.fullmatch(r"\d+[smhdw]", window):
        window = "5m"

    try:
        node_map = client.node_map()

        # ---- discovery ----
        if search:
            needle = search.strip().lower()
            names = [n for n in client.metric_names() if needle in n.lower()]
            df = pd.DataFrame({"metric_name": names})
            summary = (f"{len(names)} metric name(s) match '{search}'. "
                       + ("Matches: " + ", ".join(names[:40]) + ("…" if len(names) > 40 else "")
                          if names else "No metric names matched — try a shorter keyword."))
            return {"summary": summary, "dataframe": df, "raw": {"metric_names": names}}

        wanted = (metric or "").strip().lower()

        # ---- scrape target health ----
        if wanted in ("targets", "target", "exporters", "scrape"):
            targets = client.targets()
            rows = [{
                "job": t.get("labels", {}).get("job", ""),
                "instance": t.get("labels", {}).get("instance", ""),
                "health": t.get("health", ""),
                "last_error": (t.get("lastError") or "")[:120],
                "last_scrape": t.get("lastScrape", ""),
            } for t in targets]
            if job_filter:
                rows = [r for r in rows if job_filter.strip().lower() in r["job"].lower()]
            df = pd.DataFrame(rows)
            healthy = sum(1 for r in rows if r["health"] == "up")
            summary = (f"Prometheus is scraping {len(rows)} target(s): {healthy} healthy, "
                       f"{len(rows) - healthy} unhealthy. ")
            if not df.empty:
                summary += f"By job: {df['job'].value_counts().to_dict()}. "
                broken = [r for r in rows if r["health"] != "up"]
                summary += ("Unhealthy: " + "; ".join(
                    f"{r['instance']} ({r['job']}) — {r['last_error']}" for r in broken[:10])
                    if broken else "Every target is currently up.")
            return {"summary": summary, "dataframe": df, "raw": {"targets": rows}}

        # ---- alerts ----
        if wanted in ("alerts", "alert", "firing"):
            alerts = client.alerts()
            rows = [{
                "alert": a.get("labels", {}).get("alertname", ""),
                "state": a.get("state", ""),
                "severity": a.get("labels", {}).get("severity", ""),
                "instance": a.get("labels", {}).get("instance", ""),
                "since": a.get("activeAt", ""),
                "summary": (a.get("annotations", {}) or {}).get("summary", "")[:120],
            } for a in alerts]
            rule_count = sum(len(g.get("rules", [])) for g in client.rules())
            df = pd.DataFrame(rows)
            if rows:
                summary = (f"{len(rows)} alert(s) active. "
                           f"States: {df['state'].value_counts().to_dict()}. "
                           + "; ".join(f"{r['alert']} ({r['state']}) on {r['instance']}"
                                       for r in rows[:10]))
            else:
                summary = (f"No alerts are firing. NOTE: this Prometheus has {rule_count} "
                           f"alerting rule(s) configured"
                           + (", so no alert can ever fire — alerting has not been set up. "
                              "Report current health using metric='targets' or metric='up' instead."
                              if rule_count == 0 else "."))
            return {"summary": summary, "dataframe": df, "raw": {"alerts": rows}}

        # ---- virtual machines ----
        if wanted in ("vms", "vm", "virtual_machines", "instances", "vm_list"):
            rows = _vm_inventory(client)
            if host_filter:
                needle = host_filter.strip().lower()
                rows = [r for r in rows if needle in r["vm"].lower()
                        or needle in r["domain"].lower() or needle in r["hypervisor"].lower()]
            df = pd.DataFrame(rows)
            running = [r for r in rows if r["running"]]
            windows = [r for r in rows if r["os_guess"].startswith("Windows")]
            linux = [r for r in rows if r["os_guess"].startswith("Linux")]
            unknown = [r for r in rows if r["os_guess"] == "unknown"]

            summary = (f"{len(rows)} VM(s) known to OpenStack/libvirt; {len(running)} running, "
                       f"{len(rows) - len(running)} not running. ")
            summary += ("VMs: " + "; ".join(
                f"{r['vm']} ({'running' if r['running'] else r['state'] or 'not running'}, "
                f"on {r['hypervisor'] or '?'}, flavor {r['flavor'] or '?'})" for r in rows) + ". ")
            summary += (f"OS split INFERRED FROM NAMES ONLY — Prometheus exposes no guest-OS "
                        f"label, os_type is 'hvm' for every VM: "
                        f"likely Windows {len(windows)} ({', '.join(r['vm'] for r in windows) or 'none'}), "
                        f"likely Linux {len(linux)} ({', '.join(r['vm'] for r in linux) or 'none'}), "
                        f"undetermined {len(unknown)} ({', '.join(r['vm'] for r in unknown) or 'none'}). "
                        f"State this is an inference, not a reported fact.")
            if rows and not all(r["running"] for r in rows):
                stopped = [r["vm"] for r in rows if not r["running"]]
                summary += f" NOT RUNNING: {', '.join(stopped)}."
            return {"summary": summary, "dataframe": df, "raw": {"vms": rows}}

        # ---- VM resource utilisation ----
        if wanted in ("vm_cpu", "vm_memory", "vm_disk", "vm_utilization", "vm_usage", "vm_stats"):
            inventory = {r["domain"]: r for r in _vm_inventory(client)}
            # libvirt reports CPU time summed over every vCPU, so a busy 8-vCPU VM
            # reads as 800%. Dividing by the vCPU count makes VMs of different
            # sizes comparable, which is what "which VM is busiest" needs.
            vcpus = {s["metric"].get("domain"): max(float(s["value"][1]), 1.0)
                     for s in client.instant("libvirt_domain_info_virtual_cpus")}
            cpu = {}
            for s in client.instant(
                    f"rate(libvirt_domain_info_cpu_time_seconds_total[{window}])"):
                domain = s["metric"].get("domain")
                cpu[domain] = float(s["value"][1]) * 100 / vcpus.get(domain, 1.0)
            mem = {s["metric"].get("domain"): float(s["value"][1])
                   for s in client.instant("libvirt_domain_memory_stats_used_percent")}
            cap = {}
            for s in client.instant("sum by (domain) (libvirt_domain_block_stats_capacity_bytes)"):
                cap[s["metric"].get("domain")] = float(s["value"][1]) / (1024 ** 3)

            rows = []
            for domain, info in inventory.items():
                rows.append({
                    "vm": info["vm"],
                    "cpu_percent": round(cpu.get(domain, 0.0), 2),
                    "memory_used_percent": round(mem.get(domain, 0.0), 2),
                    "disk_capacity_gb": round(cap.get(domain, 0.0), 1),
                    "vcpus": int(vcpus.get(domain, 0)),
                    "flavor": info["flavor"],
                    "hypervisor": info["hypervisor"],
                    "running": info["running"],
                })
            if host_filter:
                needle = host_filter.strip().lower()
                rows = [r for r in rows if needle in r["vm"].lower()]
            rows.sort(key=lambda r: r["cpu_percent"], reverse=True)
            df = pd.DataFrame(rows)

            if not rows:
                return {"summary": f"No VM matched '{host_filter}'. Use metric='vms' to list them.",
                        "dataframe": df, "raw": {}}

            top_cpu = rows[0]
            top_mem = max(rows, key=lambda r: r["memory_used_percent"])
            summary = (f"VM resource utilisation ({len(rows)} VM(s), CPU averaged over {window}). "
                       f"Highest CPU: {top_cpu['vm']} at {top_cpu['cpu_percent']}%. "
                       f"Highest memory: {top_mem['vm']} at {top_mem['memory_used_percent']}%. ")
            summary += "Per VM: " + "; ".join(
                f"{r['vm']}: CPU {r['cpu_percent']}% of its {r['vcpus']} vCPU(s), "
                f"memory {r['memory_used_percent']}%, disk capacity {r['disk_capacity_gb']}GB"
                for r in rows) + ". "
            summary += ("NOTE: disk figures are PROVISIONED capacity. Guest filesystem usage "
                        "is not exposed by libvirt without a guest agent, so questions about "
                        "how full a VM's disk is CANNOT be answered from this data — say so "
                        "rather than presenting capacity as usage.")
            return {"summary": summary, "dataframe": df, "raw": {"vms": rows}}

        # ---- recently created VMs ----
        if wanted in ("vm_new", "new_vms", "vms_created", "recent_vms"):
            days = 7
            match = re.search(r"(\d+)", str(range_window or ""))
            if match:
                days = int(match.group(1))
            existing = client.instant("libvirt_domain_openstack_info")
            fresh = client.instant(
                f"libvirt_domain_openstack_info unless "
                f"(libvirt_domain_openstack_info offset {days}d)")
            names = [s["metric"].get("instance_name", s["metric"].get("domain", "?"))
                     for s in fresh]
            df = pd.DataFrame({"vm": names})
            summary = (f"{len(names)} of {len(existing)} VM(s) appeared in the last {days} days"
                       + (f": {', '.join(names)}." if names else " — none are new. ")
                       + f" Determined by comparing the VM list now against {days} days ago; "
                       f"Prometheus stores no creation timestamp, so a VM created and deleted "
                       f"within the window would not show up.")
            return {"summary": summary, "dataframe": df, "raw": {"new_vms": names}}

        # ---- neutron routers ----
        if wanted in ("routers", "router", "neutron_routers"):
            rows = []
            for s in client.instant("openstack_neutron_router"):
                labels = s["metric"]
                rows.append({
                    "router": labels.get("name", ""),
                    "status": labels.get("status", ""),
                    "admin_state_up": labels.get("admin_state_up", ""),
                    "project_id": labels.get("project_id", ""),
                })
            inactive = [r for r in rows if r["status"].upper() != "ACTIVE"]
            df = pd.DataFrame(rows)
            summary = (f"OpenStack (Neutron) virtual routers: {len(rows)} total, "
                       f"{len(rows) - len(inactive)} ACTIVE, {len(inactive)} not active. "
                       + "; ".join(f"{r['router']}: {r['status']} "
                                   f"(admin state {r['admin_state_up']})" for r in rows)
                       + ". NOTE: these are virtual routers inside OpenStack, NOT the physical "
                         "network routers monitored by Zabbix. If the user meant physical "
                         "routers, use query_zabbix with group_filter='Routers' instead.")
            return {"summary": summary, "dataframe": df, "raw": {"routers": rows}}

        # ---- networks: private vs public VM placement ----
        if wanted in ("networks", "network_placement", "vm_networks"):
            networks = {}
            for s in client.instant("openstack_neutron_network"):
                labels = s["metric"]
                networks[labels.get("id", "")] = {
                    "network": labels.get("name", ""),
                    "external": labels.get("is_external", "") == "true",
                    "shared": labels.get("is_shared", "") == "true",
                    "type": labels.get("provider_network_type", ""),
                }
            counts = {}
            for s in client.instant('openstack_neutron_port{device_owner="compute:nova"}'):
                net = s["metric"].get("network_id", "")
                counts[net] = counts.get(net, 0) + 1

            rows = []
            for net_id, info in networks.items():
                rows.append({
                    "network": info["network"],
                    "kind": "public/external" if info["external"] else "private/internal",
                    "type": info["type"],
                    "vm_ports": counts.get(net_id, 0),
                })
            rows.sort(key=lambda r: r["vm_ports"], reverse=True)
            df = pd.DataFrame(rows)
            public = sum(r["vm_ports"] for r in rows if r["kind"].startswith("public"))
            private = sum(r["vm_ports"] for r in rows if r["kind"].startswith("private"))
            summary = (f"VM network attachments: {private} on private/internal networks, "
                       f"{public} on public/external networks. By network: "
                       + "; ".join(f"{r['network']} ({r['kind']}, {r['type']}): "
                                   f"{r['vm_ports']} VM port(s)" for r in rows)
                       + ". Counted from Neutron ports owned by compute:nova — a VM attached "
                         "to both networks is counted on each.")
            return {"summary": summary, "dataframe": df, "raw": {"networks": rows}}

        # ---- volumes / snapshots / backups ----
        if wanted in ("volumes", "snapshots", "backups", "storage", "cinder"):
            rows = []
            for s in client.instant("openstack_cinder_volume_status"):
                labels = s["metric"]
                rows.append({
                    "volume": labels.get("name", "(unnamed)"),
                    "status": labels.get("status", ""),
                    "size_gb": labels.get("size", ""),
                    "bootable": labels.get("bootable", ""),
                    "attached_to_server": labels.get("server_id", "") or "(unattached)",
                })
            snaps = client.instant("openstack_cinder_snapshots")
            snap_count = int(float(snaps[0]["value"][1])) if snaps else 0
            backup_used = client.instant("sum(openstack_cinder_limits_backup_used_gb)")
            backup_gb = float(backup_used[0]["value"][1]) if backup_used else 0.0
            in_use = [r for r in rows if r["status"] == "in-use"]
            df = pd.DataFrame(rows)

            summary = (f"Cinder storage: {len(rows)} volume(s) — {len(in_use)} in use, "
                       f"{len(rows) - len(in_use)} available. "
                       f"Snapshots existing: {snap_count}. Backup space used: {backup_gb:.0f} GB. ")
            summary += "Volumes: " + "; ".join(
                f"{r['volume']} ({r['size_gb']}GB, {r['status']})" for r in rows[:15]) + ". "
            summary += ("IMPORTANT: Prometheus reports only that snapshots EXIST and how many. "
                        "It does not record whether a snapshot or backup JOB completed, when it "
                        "ran, or which VM it covered. A question about whether a backup/snapshot "
                        "succeeded CANNOT be answered from this data — say that plainly.")
            return {"summary": summary, "dataframe": df, "raw": {"volumes": rows,
                                                                 "snapshots": snap_count}}

        # ---- containers ----
        if wanted in ("containers", "container", "docker"):
            rows = []
            seen = client.instant("container_last_seen")
            for s in seen:
                labels = s["metric"]
                rows.append({
                    "container_id": (labels.get("id", "") or "")[:40],
                    "name": labels.get("name", "") or "(no name label)",
                    "image": labels.get("image", "") or "(no image label)",
                    "instance": labels.get("instance", ""),
                })
            oom = client.instant("sum by (id) (container_oom_events_total)")
            oom_total = sum(float(s["value"][1]) for s in oom)
            restarts = client.instant("sum(container_tasks_state)")
            df = pd.DataFrame(rows)
            summary = (f"cAdvisor is reporting {len(rows)} container series across the cluster. "
                       f"Total OOM events observed: {oom_total:.0f}. ")
            summary += ("IMPORTANT: this cAdvisor is NOT exposing container name or image labels "
                        "— only opaque ids. A question naming a specific container CANNOT be "
                        "matched to these metrics. Say so, and note that cAdvisor needs to be "
                        "configured with docker label collection to answer per-container "
                        "questions. There are also no container error/exit-code metrics here; "
                        "OOM events and task states are all that is available.")
            return {"summary": summary, "dataframe": df, "raw": {"containers": rows}}

        # ---- metric query ----
        sel, selb = _instance_selector(host_filter, job_filter, node_map)
        intent = _resolve_intent(metric)

        if intent:
            label, template, unit = _INTENTS[intent]
            expression = template.format(sel=sel, selb=selb, w=window)
        elif promql:
            label, unit = f"Custom query `{promql}`", ""
            expression = promql
        else:
            available = ", ".join(sorted(_INTENTS))
            return {"summary": (f"Specify what to read: metric=<one of {available}>, or "
                                f"promql=<expression>, or search=<keyword> to discover "
                                f"metric names."),
                    "dataframe": pd.DataFrame(), "raw": {}}

        series = client.instant(expression)
        rows = _rows(series, unit, node_map)
        df = pd.DataFrame(rows)

        if not rows:
            return {"summary": (f"{label}: the query returned no data. Expression used: "
                                f"{expression}. The metric may not exist for this filter — "
                                f"use search=<keyword> to check the metric name."),
                    "dataframe": df, "raw": {"promql": expression}}

        summary = f"{label} ({len(rows)} series). PromQL: {expression}. "
        values = [r["value"] for r in rows]
        if unit == "%":
            summary += (f"Highest: {rows[0]['series']} at {rows[0]['value']}%. "
                        f"Average across series: {sum(values) / len(values):.1f}%. ")
        readings = [f"{r['series']}: {r['value']}{unit}" for r in rows[:15]]
        summary += "Values: " + "; ".join(readings)
        if len(rows) > 15:
            summary += f" …and {len(rows) - 15} more series."

        return {"summary": summary, "dataframe": df, "raw": {"promql": expression, "rows": rows}}

    except Exception as exc:
        return {"summary": f"Prometheus query failed: {type(exc).__name__}: {str(exc)[:250]}",
                "dataframe": pd.DataFrame(), "raw": {"error": str(exc)}}
