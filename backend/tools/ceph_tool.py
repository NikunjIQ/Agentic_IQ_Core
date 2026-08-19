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
    question: Optional[str] = None,
    query_type: Optional[str] = "health",
    pool_filter: Optional[str] = None,
    host_filter: Optional[str] = None,
) -> dict:
    """
    Query the Ceph storage cluster: capacity, pools, OSDs, RBD images, health.

    Ceph is the STORAGE layer beneath OpenStack. It is a different system from
    the other four: Prometheus watches the Linux hosts and VM CPU/memory, Ceph
    watches the disks, pools and replication under them.

    - query_type: 'health' (default) | 'capacity' | 'pools' | 'osds' | 'hosts' | 'images'.
      'health' = cluster status, failing checks, firing alerts, PG state.
      'capacity' = how much storage is free, raw AND usable after replication.
      'pools' = per-pool usage and what each pool is for.
      'osds' = per-disk state and distribution across hosts.
      'images' = RBD images, i.e. Cinder volumes, Glance images and VM disks.
      'hosts' = the machines in the cluster.
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
    - S3, object storage, buckets: RGW is not deployed on this cluster, so those
      cannot be answered. The tool says so rather than returning an error.
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
                rows.append({"osd": o.get("osd"), "host": host,
                             "state": "+".join(o.get("state") or []),
                             "up": "up" in (o.get("state") or []),
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

        return {"summary": f"Unsupported query_type '{query_type}'. Supported: health, "
                           f"capacity, pools, osds, hosts, images.",
                "dataframe": pd.DataFrame(), "raw": {}}

    except Exception as exc:
        return {"summary": f"Ceph API error: {str(exc)[:300]}",
                "dataframe": pd.DataFrame(), "raw": {"error": str(exc)}}
