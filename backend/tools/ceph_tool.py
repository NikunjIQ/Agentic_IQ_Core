"""Query the Ceph storage cluster.

Read-only. Every figure below is computed here rather than left to the model,
for the usual reason: the numbers Ceph hands back are easy to report wrongly.

Three of those traps are load-bearing and are handled in code:

  raw vs usable   — the cluster reports 9.5 TB free, but every pool is
                    replicated, so what you can actually store is 4.44 TB on the
                    size-2 pools. Reporting raw as "free" overstates capacity by
                    the replication factor.
  pagination      — /api/block/image returns a short page by default: 5 images
                    of 37, omitting the whole `volumes` pool. The client passes
                    limit=-1; this tool states the count so a regression shows.
  pool statistics — /api/pool without stats=true reports every pool at zero
                    bytes, which reads as an empty cluster.
"""

import re
from typing import Optional

import pandas as pd
from langchain_core.tools import tool

from tools.clients import CephClient

TB = 1e12
GB = 1e9

# OpenStack names its RBD images after the object they belong to, so a UUID in
# an image name is a real foreign key into Prometheus and the OpenStack logs.
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# What each OpenStack pool holds, so an answer can name the service rather than
# the pool. These are the Kolla/OpenStack defaults and match this cluster.
_POOL_PURPOSE = {
    "images": "Glance images",
    "volumes": "Cinder volumes",
    "vms": "Nova ephemeral VM disks",
    "backups": "Cinder backups",
    ".mgr": "Ceph manager internal",
}


# Cluster-log priorities. Ceph writes [INF] for routine chatter and reserves the
# rest for anything an operator should see, so the split is Ceph's own, not ours.
_LOG_CONCERNING = ("[WRN]", "[ERR]", "[SEC]")

# Words that mark a log line as being about a failing disk rather than about
# anything else that went wrong.
_DISK_FAILURE_HINTS = ("osd", "disk", "device", "smart", "slow ops", "bluestore",
                       "read error", "failed", "down")


def _latest(stats: dict, key: str):
    """Pool statistics arrive either as a scalar or as {'latest': n}."""
    value = (stats or {}).get(key)
    if isinstance(value, dict):
        return value.get("latest")
    return value


def _fmt(num_bytes, unit=GB, suffix="GB") -> str:
    try:
        return f"{(num_bytes or 0) / unit:.2f} {suffix}"
    except (TypeError, ValueError):
        return f"0.00 {suffix}"


def _health_summary(client) -> tuple:
    health = client.health()
    status = (health.get("health") or {}).get("status", "UNKNOWN")
    checks = (health.get("health") or {}).get("checks") or []
    stats = (health.get("df") or {}).get("stats") or {}
    pg_states = (health.get("pg_info") or {}).get("statuses") or {}
    osds = (health.get("osd_map") or {}).get("osds") or []
    mons = ((health.get("mon_status") or {}).get("monmap") or {}).get("mons") or []
    quorum = (health.get("mon_status") or {}).get("quorum") or []
    return health, status, checks, stats, pg_states, osds, mons, quorum


@tool
def query_ceph(
    query_type: Optional[str] = "health",
    pool_filter: Optional[str] = None,
    host_filter: Optional[str] = None,
) -> dict:
    """
    Query the Ceph storage cluster: capacity, pools, OSDs, RBD images, health.

    Ceph is the STORAGE layer beneath OpenStack. It is a different system from
    the other four: Prometheus watches the Linux hosts and VM CPU/memory, Ceph
    watches the disks, pools and replication under them.

    - query_type: one of
      'health'    cluster status, failing checks, firing alerts, PG state.
      'capacity'  how much storage is free, raw AND usable after replication.
      'pools'     per-pool usage and what each pool is for.
      'osds'      per-disk state, host distribution, and the HDD/SSD breakdown.
      'hosts'     the machines in the cluster.
      'images'    RBD images: Cinder volumes, Glance images and VM disks.
      'snapshots' snapshot names, sizes, ages and when the most recent was taken.
      'cephfs'    filesystems, MDS daemons and their state, mounted client count.
      'crush'     CRUSH rules, the failure domain, replication per pool, and the
                  root->host->OSD hierarchy (the data a treemap is drawn from).
      'logs'      recent cluster log, with warnings/errors and disk-related
                  entries flagged separately from routine informational ones.
      'endpoints' the Grafana / Prometheus / Alertmanager URLs Ceph points at.
      'rgw'       object gateway. Not deployed here, so this states that.
    - pool_filter: limit 'images' to one pool ('volumes', 'images', 'vms', 'backups').
    - host_filter: limit 'osds' to one host ('server01', 'server02', 'server03').

    ROUTING RULES:
    - "how many VMs" is NOT a Ceph question. Ceph only knows VMs whose disk lives
      in the `vms` pool; VMs on local storage are invisible to it. For a VM count
      use query_prometheus. Ceph answers "which VMs are Ceph-backed".
    - "how much storage is free" -> query_type='capacity'. Raw free and usable
      free differ by the replication factor; the answer reports both.
    - "backups" -> query_type='pools'. Snapshots are not backups and the answer
      distinguishes them.
    - S3, object storage, buckets, RGW roles -> query_type='rgw'. RGW is not
      deployed here, so the tool states that rather than returning an error.
    - "are there disk failures" -> query_type='logs'. It returns the general log
      AND flags the concerning entries; no warnings at all is itself the answer.
    - "how many snapshots / when was the last one" -> query_type='snapshots'.
    - "crush rule", "replication details", "OSD treemap" -> query_type='crush'.
    - "Grafana URL", "Prometheus URL" -> query_type='endpoints'.
    """
    client = CephClient()
    if not client.configured:
        return {"summary": "Ceph is not configured — set CEPH_URL, CEPH_USER and "
                           "CEPH_PASS. No Ceph data can be read until then.",
                "dataframe": pd.DataFrame(), "raw": {}}

    kind = (query_type or "health").strip().lower()

    try:
        # ---------------------------------------------------------- health
        if kind in ("health", "status"):
            health, status, checks, stats, pg_states, osds, mons, quorum = _health_summary(client)
            alerts = client.alerts()
            up = sum(1 for o in osds if "up" in (o.get("state") or []))

            summary = (f"Ceph cluster status: {status}. "
                       f"{len(checks)} failing health check(s). "
                       f"OSDs: {up} up of {len(osds)}. "
                       f"Monitors: {len(mons)}, {len(quorum)} in quorum. "
                       f"Placement groups: {sum(pg_states.values())} "
                       f"({', '.join(f'{k}: {v}' for k, v in pg_states.items())}). ")
            for check in checks[:5]:
                summary += f"CHECK {check.get('type')}: {(check.get('summary') or {}).get('message')}. "

            # HEALTH_OK with an alert firing is two different truths, and only
            # reporting the first hides a real warning.
            if alerts:
                summary += f"{len(alerts)} alert(s) currently firing: "
                summary += "; ".join(
                    f"{(a.get('labels') or {}).get('alertname')} "
                    f"({(a.get('labels') or {}).get('severity')}) — "
                    f"{str((a.get('annotations') or {}).get('description', ''))[:140]}"
                    for a in alerts[:5]) + ". "
                if status == "HEALTH_OK":
                    summary += ("NOTE: the cluster reports HEALTH_OK while these alerts "
                                "are firing — Ceph health checks and Prometheus alert "
                                "rules are separate, so neither alone is the whole picture. ")
            else:
                summary += "No alerts are firing. "

            rows = [{"alert": (a.get("labels") or {}).get("alertname"),
                     "severity": (a.get("labels") or {}).get("severity"),
                     "description": (a.get("annotations") or {}).get("description")}
                    for a in alerts]
            return {"summary": summary, "dataframe": pd.DataFrame(rows),
                    "raw": {"health": health, "alerts": alerts}}

        # -------------------------------------------------------- capacity
        if kind in ("capacity", "free", "space", "usage"):
            _, status, _, stats, _, _, _, _ = _health_summary(client)
            pools = client.pools()
            rows = []
            for pool in pools:
                s = pool.get("stats") or {}
                rows.append({"pool": pool.get("pool_name"),
                             "replication": pool.get("size"),
                             "usable_free_TB": round((_latest(s, "max_avail") or 0) / TB, 2)})
            table = pd.DataFrame(rows)
            by_replication = {}
            for row in rows:
                by_replication.setdefault(row["replication"], row["usable_free_TB"])

            summary = (
                f"Raw capacity: {_fmt(stats.get('total_bytes'), TB, 'TB')} total, "
                f"{_fmt(stats.get('total_used_raw_bytes'), TB, 'TB')} used "
                f"({(stats.get('total_used_raw_ratio') or 0) * 100:.1f}%), "
                f"{_fmt(stats.get('total_avail_bytes'), TB, 'TB')} raw free. "
                f"IMPORTANT: raw free is NOT how much you can store. Every pool is "
                f"replicated, so usable free space is lower: "
                + ", ".join(f"{avail:.2f} TB on the {size}x-replicated pools"
                            for size, avail in sorted(by_replication.items()))
                + f". Cluster status: {status}.")
            return {"summary": summary, "dataframe": table,
                    "raw": {"stats": stats, "pools": rows}}

        # ----------------------------------------------------------- pools
        if kind in ("pools", "pool"):
            pools = client.pools()
            rows = []
            for pool in pools:
                s = pool.get("stats") or {}
                name = pool.get("pool_name")
                rows.append({
                    "pool": name,
                    "purpose": _POOL_PURPOSE.get(name, "—"),
                    "replication": pool.get("size"),
                    "stored_GB": round((_latest(s, "stored") or 0) / GB, 2),
                    "raw_used_GB": round((_latest(s, "bytes_used") or 0) / GB, 2),
                    "usable_free_TB": round((_latest(s, "max_avail") or 0) / TB, 2),
                    "objects": _latest(s, "objects") or 0,
                })
            table = pd.DataFrame(rows).sort_values("raw_used_GB", ascending=False)
            in_use = [r for r in rows if r["raw_used_GB"] > 0.01]
            idle = [r for r in rows if r["raw_used_GB"] <= 0.01]

            summary = (f"{len(rows)} pool(s) configured, {len(in_use)} holding data. "
                       f"Total raw used across pools: "
                       f"{sum(r['raw_used_GB'] for r in rows):.2f} GB. ")
            summary += "In use: " + "; ".join(
                f"{r['pool']} ({r['purpose']}) {r['stored_GB']:.2f} GB stored, "
                f"{r['raw_used_GB']:.2f} GB raw, {r['objects']:,} objects"
                for r in sorted(in_use, key=lambda r: -r["raw_used_GB"])) + ". "
            if idle:
                summary += ("Empty: " + ", ".join(f"{r['pool']} ({r['purpose']})"
                                                  for r in idle) + ". ")
            backups = next((r for r in rows if r["pool"] == "backups"), None)
            if backups is not None and backups["raw_used_GB"] <= 0.01:
                summary += ("NOTE: the 'backups' pool is empty — Cinder backup is "
                            "configured but nothing has been backed up. RBD snapshots "
                            "exist but a snapshot is not a backup. ")
            return {"summary": summary, "dataframe": table, "raw": {"pools": pools}}

        # ------------------------------------------------------------ osds
        if kind in ("osds", "osd", "disks"):
            osds = client.osds()
            rows = []
            for o in osds:
                host = (o.get("host") or {}).get("name", "?")
                if host_filter and host_filter.lower() not in str(host).lower():
                    continue
                stats = o.get("stats") or {}
                tree = o.get("tree") or {}
                rows.append({"osd": o.get("osd"), "host": host,
                             "state": "+".join(o.get("state") or []),
                             "up": "up" in (o.get("state") or []),
                             "device_class": tree.get("device_class") or "unknown",
                             "crush_weight": round(tree.get("crush_weight") or 0, 3),
                             "size_GB": round((stats.get("stat_bytes") or 0) / GB, 1)})
            table = pd.DataFrame(rows)
            up = sum(1 for r in rows if r["up"])
            by_host = {}
            for r in rows:
                by_host[r["host"]] = by_host.get(r["host"], 0) + 1
            scope = f" on {host_filter}" if host_filter else ""
            summary = (f"{len(rows)} OSD(s){scope}: {up} up, {len(rows) - up} not up. "
                       f"Distribution by host: "
                       f"{', '.join(f'{h}: {n}' for h, n in sorted(by_host.items()))}. "
                       f"Total raw capacity: {sum(r['size_GB'] for r in rows):.1f} GB.")
            # "How many OSDs are HDD and how many are SSD" is asked directly, and
            # the class lives in the CRUSH tree, not on the OSD record.
            classes = {}
            for r in rows:
                classes[r["device_class"]] = classes.get(r["device_class"], 0) + 1
            summary += (" By device class: "
                        + ", ".join(f"{n} {c.upper()}" for c, n in sorted(classes.items()))
                        + ".")
            if len(classes) == 1 and "hdd" in classes:
                summary += (" There are no SSD-backed OSDs in this cluster — every OSD "
                            "is spinning disk.")

            down = [r for r in rows if not r["up"]]
            if down:
                summary += (" NOT UP: " + ", ".join(f"osd.{r['osd']} on {r['host']} "
                                                    f"({r['state']})" for r in down) + ".")
            return {"summary": summary, "dataframe": table, "raw": {"osds": osds}}

        # ----------------------------------------------------------- hosts
        if kind in ("hosts", "host", "nodes"):
            hosts = client.hosts()
            rows = [{"hostname": h.get("hostname"),
                     "labels": ", ".join(h.get("labels") or []) or "—",
                     "status": h.get("status") or "ok"} for h in hosts]
            summary = (f"{len(rows)} host(s) in the Ceph cluster: "
                       + ", ".join(r["hostname"] for r in rows) + ". "
                       + "These are the same machines Prometheus monitors and "
                         "OpenSearch collects logs from, so a host name joins all three.")
            return {"summary": summary, "dataframe": pd.DataFrame(rows),
                    "raw": {"hosts": hosts}}

        # ---------------------------------------------------------- images
        if kind in ("images", "image", "rbd", "volumes", "disks_rbd"):
            groups = client.images()
            rows = []
            for group in groups:
                pool = group.get("pool_name")
                if pool_filter and pool_filter.lower() != str(pool).lower():
                    continue
                for image in (group.get("value") or []):
                    name = image.get("name") or ""
                    match = _UUID.search(name)
                    rows.append({
                        "pool": pool,
                        "image": name,
                        "purpose": _POOL_PURPOSE.get(pool, "—"),
                        "provisioned_GB": round((image.get("size") or 0) / GB, 2),
                        "used_GB": round((image.get("disk_usage") or 0) / GB, 2),
                        "snapshots": len(image.get("snapshots") or []),
                        "openstack_uuid": match.group(0) if match else None,
                    })
            table = pd.DataFrame(rows)
            scope = f" in pool '{pool_filter}'" if pool_filter else ""
            snaps = sum(r["snapshots"] for r in rows)
            by_pool = {}
            for r in rows:
                entry = by_pool.setdefault(r["pool"], {"n": 0, "prov": 0.0, "used": 0.0})
                entry["n"] += 1
                entry["prov"] += r["provisioned_GB"]
                entry["used"] += r["used_GB"]

            summary = (f"{len(rows)} RBD image(s){scope} across {len(by_pool)} pool(s), "
                       f"{snaps} snapshot(s). ")
            summary += "; ".join(
                f"{pool} ({_POOL_PURPOSE.get(pool, '—')}): {v['n']} image(s), "
                f"{v['prov']:.1f} GB provisioned, {v['used']:.1f} GB used"
                for pool, v in sorted(by_pool.items())) + ". "
            summary += ("Provisioned is what was requested; used is what is actually "
                        "consumed — RBD is thin-provisioned, so the two differ widely. ")
            vm_backed = {r["openstack_uuid"] for r in rows
                         if r["pool"] == "vms" and r["openstack_uuid"]}
            if vm_backed:
                summary += (f"{len(vm_backed)} VM(s) have their disk in Ceph. Ceph cannot "
                            f"see VMs stored on local hypervisor disk — for a total VM "
                            f"count use query_prometheus, not this tool. ")
            return {"summary": summary, "dataframe": table, "raw": {"images": groups}}

        # ------------------------------------------------------ snapshots
        if kind in ("snapshots", "snapshot", "snaps"):
            groups = client.images()
            rows = []
            for group in groups:
                pool = group.get("pool_name")
                if pool_filter and pool_filter.lower() != str(pool).lower():
                    continue
                for image in (group.get("value") or []):
                    for snap in (image.get("snapshots") or []):
                        rows.append({
                            "pool": pool,
                            "image": image.get("name"),
                            "snapshot": snap.get("name"),
                            "taken_at": snap.get("timestamp"),
                            "size_GB": round(float(snap.get("size") or 0) / GB, 2),
                            "protected": snap.get("is_protected"),
                        })
            table = pd.DataFrame(rows)
            scope = f" in pool '{pool_filter}'" if pool_filter else ""
            if not rows:
                return {"summary": f"No RBD snapshots exist{scope}.",
                        "dataframe": table, "raw": {"snapshots": rows}}

            dated = [r["taken_at"] for r in rows if r["taken_at"]]
            by_pool = {}
            for r in rows:
                by_pool[r["pool"]] = by_pool.get(r["pool"], 0) + 1
            pool_text = ", ".join(f"{p}: {n}" for p, n in sorted(by_pool.items()))
            protected = sum(1 for r in rows if r["protected"] in (True, "True", "true"))

            summary = (f"{len(rows)} RBD snapshot(s){scope} across {len(by_pool)} "
                       f"pool(s): {pool_text}. ")
            if dated:
                summary += (f"Most recent snapshot taken {max(dated)}; "
                            f"oldest {min(dated)}. ")
            summary += f"{protected} of {len(rows)} are protected. "
            summary += ("A snapshot is not a backup — it lives in the same pool as the "
                        "image it came from, so it does not survive losing that pool. "
                        "Cinder backups would land in the `backups` pool, which is empty.")
            return {"summary": summary, "dataframe": table, "raw": {"snapshots": rows}}

        # --------------------------------------------------------- cephfs
        if kind in ("cephfs", "fs", "mds", "filesystem"):
            filesystems = client.cephfs()
            rows = []
            total_clients = 0
            names = []
            for fs in filesystems:
                mdsmap = fs.get("mdsmap") or {}
                names.append(f"'{mdsmap.get('fs_name')}' "
                             f"(max_mds={mdsmap.get('max_mds')})")
                clients = client.cephfs_clients(fs.get("id"))
                total_clients += len(clients)
                daemons = mdsmap.get("info") or {}
                if not daemons:
                    rows.append({"filesystem": mdsmap.get("fs_name"),
                                 "mds": "(none up)", "state": "no active MDS",
                                 "rank": None, "address": "", "clients": len(clients)})
                for daemon in daemons.values():
                    rows.append({
                        "filesystem": mdsmap.get("fs_name"),
                        "mds": daemon.get("name"),
                        "state": daemon.get("state"),
                        "rank": daemon.get("rank"),
                        "address": str(daemon.get("addr", "")).split("/")[0],
                        "clients": len(clients),
                    })
            table = pd.DataFrame(rows)
            active = [r for r in rows if str(r.get("state", "")).startswith("up:active")]
            daemon_text = "; ".join(f"{r['mds']} {r['state']} rank {r['rank']}"
                                    for r in rows if r.get("mds") != "(none up)")

            summary = (f"{len(filesystems)} CephFS filesystem(s): {', '.join(names)}. "
                       f"{len(rows)} MDS daemon(s), {len(active)} active"
                       + (f": {daemon_text}. " if daemon_text else ". ")
                       + f"Clients with the filesystem mounted: {total_clients}. ")
            if total_clients == 0:
                summary += ("Nothing is currently mounting CephFS — the filesystem is "
                            "healthy but unused, which is different from being broken.")
            return {"summary": summary, "dataframe": table,
                    "raw": {"cephfs": filesystems}}

        # ---------------------------------------------------------- crush
        if kind in ("crush", "replication", "crush_rule", "topology", "tree"):
            rules = client.crush_rules()
            pools = client.pools()
            osds = client.osds()
            _, status, _, _, pg_states, _, _, _ = _health_summary(client)

            rule_rows = []
            for rule in rules:
                steps = rule.get("steps") or []
                choose = next((st for st in steps if st.get("type")), {})
                rule_rows.append({
                    "rule": rule.get("rule_name"),
                    "rule_id": rule.get("rule_id"),
                    "failure_domain": choose.get("type", "unknown"),
                    "root": next((st.get("item_name") for st in steps
                                  if st.get("item_name")), "default"),
                })
            root = rule_rows[0]["root"] if rule_rows else "default"
            domain = rule_rows[0]["failure_domain"] if rule_rows else "host"

            # The CRUSH hierarchy, root -> host -> osd, returned as rows so the
            # frontend can draw a treemap from it. The model reads the summary.
            tree_rows = []
            for o in osds:
                tree = o.get("tree") or {}
                tree_rows.append({
                    "root": root,
                    "host": (o.get("host") or {}).get("name", "?"),
                    "osd": f"osd.{o.get('osd')}",
                    "device_class": tree.get("device_class") or "unknown",
                    "crush_weight": round(tree.get("crush_weight") or 0, 3),
                    "size_GB": round((o.get("stats") or {}).get("stat_bytes", 0) / GB, 1),
                    "up": "up" in (o.get("state") or []),
                })

            by_size = {}
            for pool in pools:
                by_size.setdefault(pool.get("size"), []).append(pool.get("pool_name"))

            rule_text = "; ".join(f"'{r['rule']}' replicates across {r['failure_domain']}s "
                                  f"from root '{r['root']}'" for r in rule_rows)
            repl_text = "; ".join(f"{n} copies — {', '.join(sorted(names_))}"
                                  for n, names_ in sorted(by_size.items()))
            pg_text = ", ".join(f"{k}: {v}" for k, v in pg_states.items())
            hosts = {r["host"] for r in tree_rows}

            summary = (f"{len(rules)} CRUSH rule(s): {rule_text}. "
                       f"Replication by pool: {repl_text}. "
                       f"Placement group health: {pg_text}, cluster {status}. "
                       f"The hierarchy has {len(hosts)} host(s) and {len(tree_rows)} "
                       f"OSD(s), so a failure domain of '{domain}' means copies of an "
                       f"object never share a host. ")
            if by_size and min(by_size) == 2:
                summary += ("NOTE: the 2-copy pools tolerate ONE host failing. Losing a "
                            "second host before recovery finished would mean data loss.")
            return {"summary": summary, "dataframe": pd.DataFrame(tree_rows),
                    "raw": {"crush_rules": rules, "hierarchy": tree_rows,
                            "pool_replication": {str(k): v for k, v in by_size.items()}}}

        # ----------------------------------------------------------- logs
        if kind in ("logs", "log", "events", "failures"):
            payload = client.logs()
            rows = []
            for channel in ("clog", "audit_log"):
                for entry in (payload.get(channel) or []):
                    priority = str(entry.get("priority") or "").strip()
                    message = str(entry.get("message") or "")
                    rows.append({
                        "channel": channel,
                        "priority": priority,
                        "concerning": priority in _LOG_CONCERNING,
                        "disk_related": any(h in message.lower()
                                            for h in _DISK_FAILURE_HINTS),
                        "message": message[:300],
                        "at": entry.get("stamp"),
                    })
            table = pd.DataFrame(rows)
            concerning = [r for r in rows if r["concerning"]]
            disk = [r for r in concerning if r["disk_related"]]
            counts = {}
            for r in rows:
                counts[r["priority"] or "unlabelled"] = \
                    counts.get(r["priority"] or "unlabelled", 0) + 1
            count_text = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))

            summary = f"{len(rows)} recent cluster log entr(ies) ({count_text}). "
            if concerning:
                detail = " | ".join(f"[{r['priority']}] {r['message'][:120]}"
                                    for r in concerning[:6])
                summary += (f"{len(concerning)} entr(ies) are warnings or errors: "
                            f"{detail}. ")
                summary += (f"{len(disk)} of those mention a disk or OSD. " if disk
                            else "None of them mention a disk or OSD. ")
            else:
                # The absence IS the answer to "are there any disk failures", so it
                # is stated rather than left as an empty table.
                summary += ("Every entry is informational — there are no warnings or "
                            "errors in the cluster log, so NO disk failures are "
                            "recorded. This is the recent log only, not the full "
                            "history: a disk that failed long ago and was replaced "
                            "would no longer appear here. ")
            return {"summary": summary, "dataframe": table, "raw": {"logs": payload}}

        # ------------------------------------------------------ endpoints
        if kind in ("endpoints", "urls", "url", "settings", "grafana", "prometheus"):
            found = client.endpoints()
            rows = [{"setting": k, "value": v if v not in ("", None) else "(not set)"}
                    for k, v in found.items()]
            listed = "; ".join(f"{r['setting']} = {r['value']}" for r in rows)
            summary = (f"Monitoring endpoints Ceph is configured against: {listed}. "
                       f"These are configuration values, not storage data. Only these "
                       f"specific settings are read — the settings endpoint also "
                       f"returns credentials, which are never retrieved.")
            return {"summary": summary, "dataframe": pd.DataFrame(rows),
                    "raw": {"endpoints": found}}

        # ------------------------------------------------------------ rgw
        if kind in ("rgw", "roles", "buckets", "s3", "object"):
            daemons = client.rgw_daemons()
            if not daemons:
                return {"summary": ("The RADOS Gateway (RGW) is NOT deployed on this "
                                    "cluster — zero RGW daemons are running. So there "
                                    "are no S3 buckets, no object-storage users and no "
                                    "RGW roles to report. This is a deployment fact, "
                                    "not a permissions or connectivity problem: asking "
                                    "Ceph for buckets returns a server error because "
                                    "the service does not exist."),
                        "dataframe": pd.DataFrame(), "raw": {"rgw_daemons": []}}
            rows = [{"daemon": d.get("id"), "host": d.get("server_hostname"),
                     "version": d.get("version")} for d in daemons]
            return {"summary": f"{len(rows)} RGW daemon(s) running.",
                    "dataframe": pd.DataFrame(rows), "raw": {"rgw_daemons": daemons}}

        return {"summary": f"Unsupported query_type '{query_type}'. Supported: health, "
                           f"capacity, pools, osds, hosts, images, snapshots, "
                           f"cephfs, crush, logs, endpoints, rgw.",
                "dataframe": pd.DataFrame(), "raw": {}}

    except Exception as exc:
        return {"summary": f"Ceph API error: {str(exc)[:300]}",
                "dataframe": pd.DataFrame(), "raw": {"error": str(exc)}}
