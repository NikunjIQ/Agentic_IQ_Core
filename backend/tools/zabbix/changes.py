"""What changed before it broke: live Zabbix problems joined to the config audit
log on host and time.

A person editing a device shortly before it failed is the finding. Zabbix's own
template churn (username "System") is reported, but separately: on the routers
it is ~500 entries a day and would otherwise bury the one change that matters.
"""

import concurrent.futures

import pandas as pd

from tools.zabbix.audit import audit_entries, resolve_resources
from tools.zabbix.monitoring import query_problems

# ponytail: fixed 24h window; take it from the question when someone asks for another.
WINDOW_SECONDS = 24 * 3600
MAX_HOSTS = 10
# The routers log ~500 automatic changes a day, so the audit query's default
# 500 would not reach back a full window before a problem.
AUDIT_ROWS = 5000


def _windows(clocks) -> list:
    """The 24h before each problem, overlapping ones merged. One span from the
    oldest to the newest problem covered 69 days on a router and hit the row
    limit; the windows themselves are what the question is about."""
    merged = []
    for clock in sorted(clocks):
        start = clock - WINDOW_SECONDS
        if merged and start <= merged[-1][1]:
            merged[-1][1] = clock
        else:
            merged.append([start, clock])
    return merged


def _audit_for(rpc, host, clocks) -> tuple:
    """(audit rows in this host's windows, capped?, has any audit trail at all?)"""
    matched, types = resolve_resources(rpc, host.split(",")[0].strip())
    if not matched:
        return [], False, False
    rows, capped = [], False
    for start, end in _windows(clocks):
        batch = audit_entries(rpc, types, start, end, AUDIT_ROWS)
        capped |= len(batch) >= AUDIT_ROWS
        rows += batch
    # Nothing in the windows, or nothing ever? Only the first means no change.
    return rows, capped, bool(rows) or bool(audit_entries(rpc, types, limit=1))


def query_changes(rpc, host_filter=None, status_filter=None) -> dict:
    problems = query_problems(rpc, host_filter=host_filter,
                              status_filter=status_filter)["dataframe"]
    if problems.empty:
        return {"summary": "There are no active problems to look for changes before.",
                "dataframe": pd.DataFrame(), "raw": {}}

    problems = problems.assign(clock=pd.to_numeric(problems["clock"]),
                               severity=pd.to_numeric(problems["severity"]))
    hosts = list(problems.sort_values("severity", ascending=False)["host"].drop_duplicates())
    checked, skipped = hosts[:MAX_HOSTS], hosts[MAX_HOSTS:]

    # Four or five Zabbix calls per host, each ~1s; one host after another took
    # 137s for the whole estate.
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        audits = dict(zip(checked, pool.map(
            lambda h: _audit_for(rpc, h, problems[problems["host"] == h]["clock"]), checked)))

    rows = []
    no_trail = [h for h in checked if not audits[h][2]]
    capped = [h for h in checked if audits[h][1]]
    for host in checked:
        audit = pd.DataFrame(audits[host][0])
        if audit.empty:
            continue
        audit["clock"] = pd.to_numeric(audit["clock"])
        for _, p in problems[problems["host"] == host].iterrows():
            window = audit[(audit["clock"] <= p["clock"])
                           & (audit["clock"] >= p["clock"] - WINDOW_SECONDS)]
            base = {"host": host, "problem": p["name"], "severity": p["severity_label"],
                    "problem_started": pd.to_datetime(p["clock"], unit="s")}
            by_system = window["username"].astype(str).str.lower() == "system"
            for _, a in window[~by_system].iterrows():
                rows.append({**base, "change": a["change"], "resource": a["resource"],
                             "name": a.get("resourcename"), "by": a.get("username"),
                             "by_person": True,
                             "minutes_before": round((p["clock"] - a["clock"]) / 60)})
            # Zabbix's own churn: one row per problem, counted, not one per entry —
            # 1,681 rows of template updates buried the one change a person made.
            auto = window[by_system]
            if not auto.empty:
                rows.append({**base, "change": f"{len(auto)} automatic change(s)",
                             "resource": auto["resource"].mode().iat[0], "name": "",
                             "by": "System", "by_person": False,
                             "minutes_before": round((p["clock"] - auto["clock"].max()) / 60)})

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["by_person", "minutes_before"], ascending=[False, True])

    # Per problem: did a person change something, only Zabbix itself, or nothing?
    keys = ["host", "problem", "problem_started"]
    person = df[df["by_person"]].drop_duplicates(keys) if not df.empty else df
    touched = df.drop_duplicates(keys) if not df.empty else df
    in_scope = problems[problems["host"].isin(checked) & ~problems["host"].isin(no_trail)]
    automatic_only = len(touched) - len(person)
    untouched = len(in_scope) - len(touched)

    parts = [f"Checked configuration changes in the 24 hours before each of "
             f"{len(in_scope)} active problem(s) on {len(checked) - len(no_trail)} host(s)."]
    if not person.empty:
        parts.append(f"{len(person)} problem(s) had a change made BY A PERSON beforehand: "
                     + " | ".join(f"{r['host']}: '{r['problem']}' — {r['by']} {r['change']} "
                                  f"{r['resource']} '{r['name']}' {r['minutes_before']} min before"
                                  for _, r in person.head(8).iterrows()) + ".")
    else:
        parts.append("No person changed any of these devices in the 24 hours before "
                     "their problems.")
    parts.append(f"{automatic_only} problem(s) were preceded only by automatic changes "
                 f"(Zabbix itself, e.g. template updates); {untouched} by no change at all.")
    if no_trail:
        parts.append(f"No audit trail exists at all for {', '.join(no_trail)}, so a change "
                     f"there cannot be ruled out — this is not 'nothing changed'.")
    if capped:
        parts.append(f"The audit log hit its {AUDIT_ROWS}-row limit for {', '.join(capped)}; "
                     f"counts there are minimums.")
    if skipped:
        parts.append(f"Not checked (limit of {MAX_HOSTS} hosts, most severe first): "
                     f"{', '.join(skipped)}.")

    return {"summary": " ".join(parts), "dataframe": df,
            "raw": {"no_audit_trail": no_trail, "capped": capped, "skipped": skipped}}
