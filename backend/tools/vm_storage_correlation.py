"""
Deterministic correlation between Ceph-backed VM storage and Prometheus VM
compute metrics.

OpenStack names a Ceph RBD image after the instance it belongs to —
`<uuid>_disk` in the `vms` pool. Prometheus's libvirt exporter carries the
same UUID as the `instance_id` label on `libvirt_domain_openstack_info`. Both
sides were queried live and matched before this was written: one VM,
`jammy`, had its UUID appear identically on both.

Most VMs will NOT match. Only a VM whose disk was explicitly provisioned on
Ceph appears in the `vms` pool at all — the rest run on local hypervisor
storage, which Ceph cannot see by any means. That absence is not a gap in
this tool; it is usually the answer. Every VM Prometheus knows about gets a
row here, matched or not, for the same reason correlation.py keeps a "NO
TICKET" row instead of dropping it.
"""

import re
import time
from typing import Optional

import pandas as pd
from langchain_core.tools import tool

from tools.clients import CephClient, PrometheusClient

GB = 1e9

# Same pattern as ceph_tool.py's _UUID, copied rather than imported: importing
# from a tool module into this one would run the dependency the wrong way, and
# a five-line regex is cheaper to duplicate than to refactor into a shared spot
# for one caller.
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

_CACHE = {"data": None, "fetched_at": 0.0}
CACHE_TTL = 60


def _ceph_vm_images(images: list) -> dict:
    """instance uuid -> {pool, image, provisioned_GB, used_GB}, vms pool only.

    Only the `vms` pool is Nova ephemeral disk; `images` is Glance and `volumes`
    is Cinder. A UUID can appear in a Glance image name too (it is how Glance
    names uploads), so scoping to `vms` is what keeps this a VM-storage join
    rather than a coincidental string match.
    """
    by_uuid = {}
    for group in images:
        if group.get("pool_name") != "vms":
            continue
        for image in (group.get("value") or []):
            name = image.get("name") or ""
            match = _UUID.search(name)
            if not match:
                continue
            by_uuid[match.group(0)] = {
                "pool": "vms",
                "image": name,
                "provisioned_GB": round((image.get("size") or 0) / GB, 2),
                "used_GB": round((image.get("disk_usage") or 0) / GB, 2),
            }
    return by_uuid


def _prometheus_vm_compute(client, domain: str, vcpu_count: float) -> dict:
    """CPU%, memory% and disk capacity for one VM, by its libvirt domain id.

    Reuses the exact correction prometheus_tool.py's vm_utilization branch
    applies: libvirt sums CPU time across every vCPU, so an 8-vCPU VM under
    light load still reads triple digits unless it is divided back down. Not
    imported from there because that function returns a formatted tool
    response, not raw numbers — the PromQL itself is what is shared here, not
    the function.
    """
    domain_filter = '{domain="' + domain + '"}'
    cpu_percent = 0.0
    for s in client.instant(
            "rate(libvirt_domain_info_cpu_time_seconds_total" + domain_filter + "[5m])"):
        cpu_percent = float(s["value"][1]) * 100 / max(vcpu_count, 1.0)
        break
    memory_percent = 0.0
    for s in client.instant(
            "libvirt_domain_memory_stats_used_percent" + domain_filter):
        memory_percent = float(s["value"][1])
        break
    return {"cpu_percent": round(cpu_percent, 2),
            "memory_used_percent": round(memory_percent, 2)}


def correlate_vm_storage(vm_filter: Optional[str] = None) -> pd.DataFrame:
    """One row per VM Prometheus knows about, joined to its Ceph disk if any."""
    now = time.time()
    if (_CACHE["data"] is not None and vm_filter is None
            and now - _CACHE["fetched_at"] < CACHE_TTL):
        return _CACHE["data"]

    from tools.prometheus_tool import _vm_inventory
    prom = PrometheusClient()
    inventory = _vm_inventory(prom)

    ceph = CephClient()
    ceph_by_uuid = _ceph_vm_images(ceph.images())

    vcpus = {s["metric"].get("domain"): max(float(s["value"][1]), 1.0)
             for s in prom.instant("libvirt_domain_info_virtual_cpus")}

    rows = []
    for vm in inventory:
        if vm_filter and vm_filter.strip().lower() not in vm["vm"].lower():
            continue
        uuid = vm.get("instance_id") or ""
        ceph_disk = ceph_by_uuid.get(uuid)

        row = {
            "vm": vm["vm"],
            "domain": vm["domain"],
            "instance_id": uuid,
            "running": vm["running"],
            "hypervisor": vm["hypervisor"],
            "on_ceph": ceph_disk is not None,
        }
        if ceph_disk:
            row["ceph_pool"] = ceph_disk["pool"]
            row["ceph_provisioned_GB"] = ceph_disk["provisioned_GB"]
            row["ceph_used_GB"] = ceph_disk["used_GB"]
            compute = _prometheus_vm_compute(prom, vm["domain"], vcpus.get(vm["domain"], 1.0))
            row.update(compute)
        else:
            row["ceph_pool"] = None
            row["ceph_provisioned_GB"] = None
            row["ceph_used_GB"] = None
            row["cpu_percent"] = None
            row["memory_used_percent"] = None
        rows.append(row)

    df = pd.DataFrame(rows)
    if vm_filter is None:
        _CACHE["data"] = df
        _CACHE["fetched_at"] = now
    return df


@tool
def correlate_vm_storage_and_compute(vm_filter: Optional[str] = None) -> dict:
    """
    For each VM, say whether its disk lives on Ceph and, if so, join that to its
    live CPU/memory from Prometheus in ONE call.

    Use this for ANY question spanning both — "is jammy on Ceph and how busy is
    it", "which VMs are Ceph-backed", "how much Ceph storage does this VM use".
    Do NOT call query_ceph and query_prometheus separately and try to match VM
    names yourself: most VMs run on local hypervisor disk, invisible to Ceph,
    and that absence is usually the actual answer — a manual match would just
    see two unrelated tables.

    - vm_filter: Optional. A VM name or partial name (e.g. 'jammy').
    """
    try:
        df = correlate_vm_storage(vm_filter)
    except Exception as e:
        return {"summary": f"Correlation failed: {e}", "dataframe": pd.DataFrame(),
                "raw": {"error": str(e)}}

    if df.empty:
        scope = f" matching '{vm_filter}'" if vm_filter else ""
        return {"summary": f"No VMs found{scope}.", "dataframe": df, "raw": {}}

    on_ceph = df[df["on_ceph"]]
    off_ceph = df[~df["on_ceph"]]

    parts = [
        f"{len(df)} VM(s) checked: {len(on_ceph)} have their disk on Ceph, "
        f"{len(off_ceph)} run on local hypervisor storage, invisible to Ceph. "
        "Matched on the instance UUID Ceph names its RBD image after and "
        "Prometheus carries as instance_id — an exact key, not a name guess."
    ]

    if not on_ceph.empty:
        lines = []
        for _, r in on_ceph.iterrows():
            line = (f"{r['vm']}: {r['ceph_used_GB']:.1f}/{r['ceph_provisioned_GB']:.1f} GB "
                    f"used on Ceph ({r['ceph_pool']} pool)")
            if r["running"]:
                line += f", CPU {r['cpu_percent']}%, memory {r['memory_used_percent']}%"
            else:
                line += ", not running"
            lines.append(line)
        parts.append("ON CEPH:\n" + "\n".join(lines))

    if not off_ceph.empty:
        parts.append("NOT ON CEPH (local hypervisor disk): "
                     + ", ".join(off_ceph["vm"]) + ".")

    return {"summary": "\n".join(parts), "dataframe": df,
            "raw": {"on_ceph": len(on_ceph), "off_ceph": len(off_ceph)}}
