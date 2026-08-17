"""Service levels: SLA targets and the achieved SLI per service.

Zabbix attaches an SLA to services by tag, and the services in this estate are
tagged with the device IP. People ask by device name, so the lookup goes through
the shared device resolver rather than matching the string as typed.
"""

import pandas as pd

# SLA reporting periods, as sla.get returns them.
SLA_PERIOD_LABELS = {"0": "daily", "1": "weekly", "2": "monthly",
                     "3": "quarterly", "4": "annually"}

# How many past reporting periods to score an SLA over. Enough to say "it has
# been met every day this week" rather than only reporting the period in
# progress, which is always the least informative one.
SLA_PERIODS = 7


def _achieved(cell):
    """The achieved percentage, or None where none was measured.

    Zabbix returns sli = -1 for a period that falls before the SLA's effective
    date. It is a sentinel, not a reading, and comparing it against the target
    scores a service that has never been down as breaching.
    """
    try:
        value = float(cell.get("sli"))
    except (TypeError, ValueError):
        return None
    return None if value < 0 else value


def query_sla(rpc, host_filter=None) -> dict:
    slas = rpc("sla.get", {"output": "extend", "selectServiceTags": "extend"})
    services = rpc("service.get", {
        "output": ["serviceid", "name", "status"],
        "selectTags": "extend",
        "selectParents": ["serviceid", "name"],
    })
    svc_by_id = {str(s["serviceid"]): s for s in services}

    if not slas:
        summary = (
            f"No SLAs are configured in Zabbix, so no service level can be "
            f"reported. Zabbix only computes an SLI where an SLA definition "
            f"exists and is attached to services by tag. "
            f"{len(services)} service(s) are configured "
            f"({', '.join(s.get('name', '') for s in services[:10]) or 'none'}), "
            f"but without an SLA none of them has a target or an achieved figure."
        )
        return {"summary": summary, "dataframe": pd.DataFrame(services),
                "raw": {"slas": [], "services": services}}

    # Scope to one device if asked. resolve_device turns a name, a visible name
    # or any interface IP into one identity, which is what lets "Nikunj VM"
    # reach the service tagged with its IP.
    target = None
    aliases = set()
    if host_filter:
        from tools.correlation import resolve_device
        target = resolve_device(host_filter)
        if target:
            aliases = {str(a).lower() for a in target.get("aliases", [])}
        else:
            aliases = {host_filter.strip().lower()}

    rows = []
    sla_headlines = []
    for sla in slas:
        period_label = SLA_PERIOD_LABELS.get(str(sla.get("period")),
                                             f"period {sla.get('period')}")
        try:
            slo = float(sla.get("slo"))
        except (TypeError, ValueError):
            slo = None
        sla_headlines.append(
            f"'{sla.get('name')}' ({period_label}, target "
            f"{slo if slo is not None else '?'}%)")

        # Omitting serviceids makes Zabbix resolve the SLA's service tags itself,
        # so the coverage is Zabbix's own answer rather than our
        # re-implementation of its tag matching.
        try:
            sli_data = rpc("sla.getsli", {"slaid": sla["slaid"], "periods": SLA_PERIODS})
        except Exception as sli_error:
            rows.append({"sla": sla.get("name"), "period_type": period_label,
                         "slo_target_pct": slo, "service": "(all)",
                         "error": str(sli_error)})
            continue

        periods = sli_data.get("periods") or []
        service_ids = [str(s) for s in (sli_data.get("serviceids") or [])]
        matrix = sli_data.get("sli") or []

        for col, sid in enumerate(service_ids):
            svc = svc_by_id.get(sid, {})
            svc_name = svc.get("name") or f"service {sid}"
            tag_values = {str(t.get("value", "")).lower() for t in (svc.get("tags") or [])}
            if aliases and not (svc_name.lower() in aliases or (tag_values & aliases)):
                continue

            cells = [matrix[r][col] for r in range(len(periods))
                     if r < len(matrix) and col < len(matrix[r])]
            if not cells:
                continue

            latest = cells[-1]
            scored = [v for v in (_achieved(c) for c in cells) if v is not None]
            unmeasured = len(cells) - len(scored)
            breaches = [v for v in scored if v < slo] if slo is not None else []
            window = periods[-1] if periods else {}

            rows.append({
                "sla": sla.get("name"),
                "period_type": period_label,
                "slo_target_pct": slo,
                "service": svc_name,
                "current_sli_pct": _achieved(latest),
                "meets_target": (None if slo is None or _achieved(latest) is None
                                 else _achieved(latest) >= slo),
                "downtime_s": latest.get("downtime"),
                "uptime_s": latest.get("uptime"),
                "error_budget_s": latest.get("error_budget"),
                "periods_scored": len(scored),
                "periods_below_target": len(breaches),
                "periods_not_measured": unmeasured,
                "current_period_from": pd.to_datetime(window.get("period_from"),
                                                      unit="s", errors="coerce"),
                "current_period_to": pd.to_datetime(window.get("period_to"),
                                                    unit="s", errors="coerce"),
            })

    df = pd.DataFrame(rows)
    scope = ""
    if host_filter:
        label = (target or {}).get("name") or host_filter
        scope = f" for {label}"

    if df.empty:
        summary = (
            f"{len(slas)} SLA(s) are configured ({'; '.join(sla_headlines)}), but "
            f"none of them covers any service matching '{host_filter}'. Zabbix "
            f"attaches an SLA to services by tag, so a device with no tagged "
            f"service has no service level defined — that is a configuration gap, "
            f"not an outage."
        )
    else:
        measured = df[df["current_sli_pct"].notna()] if "current_sli_pct" in df else df
        failing = (measured[measured["meets_target"] == False]
                   if "meets_target" in measured else pd.DataFrame())
        summary = (
            f"{len(slas)} SLA(s) configured{scope}: {'; '.join(sla_headlines)}. "
            f"{len(measured)} SLA-to-service measurement(s) across "
            f"{measured['service'].nunique() if not measured.empty else 0} service(s). "
        )
        if not measured.empty:
            summary += (f"Currently meeting target: "
                        f"{int((measured['meets_target'] == True).sum())}; "
                        f"below target: {len(failing)}. ")
            detail = [
                f"{r['service']} — {r['sla']} ({r['period_type']}): "
                f"{r['current_sli_pct']}% against a {r['slo_target_pct']}% target, "
                f"downtime {r['downtime_s']}s"
                + (f", below target in {r['periods_below_target']} of the last "
                   f"{r['periods_scored']} period(s)"
                   if r['periods_below_target'] else
                   f", met in all {r['periods_scored']} period(s) measured")
                + (f" ({r['periods_not_measured']} earlier period(s) predate the SLA "
                   f"and were not measured)" if r['periods_not_measured'] else "")
                for _, r in measured.head(12).iterrows()
            ]
            summary += " | ".join(detail)
            if len(measured) > 12:
                summary += f" | ...and {len(measured) - 12} more measurement(s)."
        if not failing.empty:
            summary += (" BELOW TARGET: " + ", ".join(
                f"{r['service']} ({r['current_sli_pct']}% vs {r['slo_target_pct']}%)"
                for _, r in failing.iterrows()) + ".")

    # Services exist that no SLA scores. Saying so separates "meeting its target"
    # from "never measured", which otherwise look alike.
    covered = set(df["service"]) if not df.empty else set()
    unscored = [s.get("name") for s in services if s.get("name") not in covered]
    if unscored and not host_filter:
        summary += (f" {len(unscored)} configured service(s) are not covered by any "
                    f"SLA and therefore have no measured service level: "
                    f"{', '.join(unscored[:10])}.")

    return {"summary": summary, "dataframe": df,
            "raw": {"slas": slas, "services": services,
                    "measurements": df.to_dict("records") if not df.empty else []}}
