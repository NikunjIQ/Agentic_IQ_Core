"""Tool-layer tests against the live estate. No LLM, so no token cost.

Two kinds of check:

1. COVERAGE — every question the assistant is expected to answer returns usable
   data through the arguments that should answer it.
2. CONSISTENCY — different routes to the same fact agree. This is the class of
   bug that actually shipped: "how many devices are down" answered 0 while
   "which group has the most down devices" answered 2, from the same estate.

Assertions are about shape and internal agreement, never exact live values.
"""

import re

import conftest  # noqa: F401
from golden_queries import (ZABBIX_QUERIES, ZAMMAD_QUERIES, PROMETHEUS_QUERIES,
                            OPENSEARCH_QUERIES, ZAMMAD_SUMMARY_MUST_MENTION,
                            HONESTY_EXPECTATIONS)


def _number_after(summary: str, label: str):
    match = re.search(re.escape(label) + r"\s*([0-9]+)", summary)
    return int(match.group(1)) if match else None


# --- coverage ---------------------------------------------------------------
def test_zabbix_answers_every_expected_question():
    from tools.zabbix_tool import query_zabbix
    for label, args, must_contain in ZABBIX_QUERIES:
        result = query_zabbix.invoke(dict(args))
        summary = str(result["summary"])
        assert "Error" not in summary[:40], f"{label}: {summary[:120]}"
        assert must_contain in summary, f"{label}: expected {must_contain!r} in {summary[:160]!r}"


def test_zammad_answers_every_expected_question():
    from tools.zammad_tool import query_zammad
    for label, args, must_contain in ZAMMAD_QUERIES:
        result = query_zammad.invoke(dict(args))
        summary = str(result["summary"])
        assert must_contain in summary, f"{label}: expected {must_contain!r}"


def test_zammad_summary_always_carries_the_headline_statistics():
    """Several expected questions are answered from the summary, not a filter."""
    from tools.zammad_tool import query_zammad
    summary = str(query_zammad.invoke({})["summary"])
    for phrase in ZAMMAD_SUMMARY_MUST_MENTION:
        assert phrase in summary, f"summary no longer reports {phrase!r}"


def test_prometheus_answers_every_expected_question():
    from tools.prometheus_tool import query_prometheus
    for label, args, must_contain in PROMETHEUS_QUERIES:
        result = query_prometheus.invoke(dict(args))
        summary = str(result["summary"])
        assert "failed" not in summary[:40].lower(), f"{label}: {summary[:140]}"
        assert must_contain in summary, f"{label}: expected {must_contain!r} in {summary[:160]!r}"


def test_opensearch_answers_every_expected_question():
    from tools.opensearch_tool import query_opensearch
    for label, args, must_contain in OPENSEARCH_QUERIES:
        result = query_opensearch.invoke(dict(args))
        summary = str(result["summary"])
        assert "failed" not in summary[:40].lower(), f"{label}: {summary[:140]}"
        assert must_contain in summary, f"{label}: expected {must_contain!r} in {summary[:180]!r}"


def test_opensearch_level_counts_reconcile_with_the_total():
    """Access logs carry no level, so the breakdown must account for the gap."""
    from tools.opensearch_tool import query_opensearch
    result = query_opensearch.invoke({"date_filter": "yesterday"})
    summary = str(result["summary"])
    total = int(re.search(r"for yesterday: ([\d,]+) entries", summary).group(1).replace(",", ""))
    levelled = sum(result["raw"]["levels"].values())
    if levelled < total:
        assert "no level field" in summary, (
            f"{total - levelled:,} entries are unaccounted for and the summary does not say so")


def test_opensearch_normalises_log_level_case():
    """'error' and 'ERROR' are the same severity; counting one is wrong by ~45x."""
    from tools.opensearch_tool import query_opensearch
    result = query_opensearch.invoke({"log_level": "error", "date_filter": "last 7 days"})
    levels = result["raw"].get("levels", {})
    assert all(k == k.upper() for k in levels), f"levels not folded to one case: {levels}"
    assert not ({"error", "Error", "ERR"} & set(levels)), f"unfolded variants present: {levels}"


def test_opensearch_redirects_device_questions_to_zabbix():
    """Zero hits for a Cisco device must not read as 'nothing is wrong'."""
    from tools.opensearch_tool import query_opensearch
    summary = str(query_opensearch.invoke(
        {"search": "FastEthernet interface down", "date_filter": "last 7 days"})["summary"])
    assert "query_zabbix" in summary
    assert "does NOT mean nothing is wrong" in summary


def test_opensearch_plain_zero_is_not_a_device_redirect():
    """An ordinary empty result should not lecture about network devices."""
    from tools.opensearch_tool import query_opensearch
    summary = str(query_opensearch.invoke(
        {"search": "zzzznotarealtokenzzzz", "date_filter": "today"})["summary"])
    assert "real zero" in summary and "query_zabbix" not in summary


def test_opensearch_date_windows_differ():
    """Guards the date parser: today and yesterday must not resolve the same."""
    from tools.opensearch_tool import query_opensearch
    today = query_opensearch.invoke({"date_filter": "today"})["raw"]["query"]
    yesterday = query_opensearch.invoke({"date_filter": "yesterday"})["raw"]["query"]
    assert today != yesterday


def test_tools_state_their_limits_instead_of_fabricating():
    from tools.prometheus_tool import query_prometheus
    for args, phrase in HONESTY_EXPECTATIONS:
        summary = str(query_prometheus.invoke(dict(args))["summary"])
        assert phrase in summary, (
            f"{args} no longer discloses its limitation ({phrase!r}) — the model "
            f"will present this as fact")


# --- consistency: the bugs that actually shipped ----------------------------
def test_down_host_count_agrees_between_hosts_and_hostgroups():
    """The original contradiction: hosts said 0 down, host groups said 2."""
    from tools.zabbix_tool import query_zabbix
    hosts = query_zabbix.invoke({"query_type": "hosts"})
    groups = query_zabbix.invoke({"query_type": "hostgroups"})

    down_names = set(hosts["dataframe"].loc[
        hosts["dataframe"]["available_str"].isin(("down", "unreachable")), "name"])
    group_df = groups["dataframe"]
    named_in_groups = set()
    for value in group_df.get("down_device_names", []):
        named_in_groups.update(n.strip() for n in str(value).split(",") if n.strip())

    assert named_in_groups <= down_names, (
        f"host groups report hosts as down that the host view does not: "
        f"{named_in_groups - down_names}")


def test_status_filter_matches_the_unfiltered_count():
    from tools.zabbix_tool import query_zabbix
    everything = query_zabbix.invoke({"query_type": "hosts"})
    reported_down = _number_after(str(everything["summary"]), "Down (not responding):")
    filtered = query_zabbix.invoke({"query_type": "hosts", "status_filter": "down"})
    assert reported_down == len(filtered["dataframe"]), (
        f"summary says {reported_down} down, filter returns {len(filtered['dataframe'])}")


def test_multi_status_filter_is_the_union_of_its_parts():
    """'unreachable or unknown' used to silently return only unreachable."""
    from tools.zabbix_tool import query_zabbix
    unreachable = len(query_zabbix.invoke(
        {"query_type": "hosts", "status_filter": "unreachable"})["dataframe"])
    unknown = len(query_zabbix.invoke(
        {"query_type": "hosts", "status_filter": "unknown"})["dataframe"])
    both = len(query_zabbix.invoke(
        {"query_type": "hosts", "status_filter": "unreachable or unknown"})["dataframe"])
    assert both == unreachable + unknown, (
        f"union returned {both}, parts are {unreachable} + {unknown}")


def test_multi_metric_keyword_returns_every_metric_named():
    """'cpu & memory utilization' used to answer with CPU only."""
    from tools.zabbix_tool import query_zabbix
    result = query_zabbix.invoke({"query_type": "items", "host_filter": "192.168.1.73",
                                  "item_keyword": "cpu & memory"})
    metrics = set(result["dataframe"]["metric"])
    assert {"CPU", "Memory"} <= metrics, f"got {metrics}"


def test_traffic_returns_throughput_not_discard_counters():
    """A traffic question used to answer with 'Inbound packets discarded'."""
    from tools.zabbix_tool import query_zabbix
    result = query_zabbix.invoke({"query_type": "items", "host_filter": "192.168.1.73",
                                  "item_keyword": "traffic"})
    keys = {k.split("[")[0] for k in result["dataframe"]["key_"]}
    assert keys <= {"net.if.in", "net.if.out"}, f"non-throughput keys leaked in: {keys}"


def test_preview_table_agrees_with_the_summary():
    """The model once noticed the table disagreed with the prose. It was right."""
    from tools.zabbix_tool import query_zabbix
    result = query_zabbix.invoke({"query_type": "items", "host_filter": "192.168.1.73",
                                  "item_keyword": "traffic"})
    assert "Network traffic" in str(result["summary"])
    assert set(result["dataframe"]["metric"]) == {"Network traffic (bits in/out)"}


def test_problems_can_be_scoped_to_one_device():
    from tools.zabbix_tool import query_zabbix
    everything = query_zabbix.invoke({"query_type": "problems"})
    scoped = query_zabbix.invoke({"query_type": "problems", "host_filter": "192.168.1.74"})
    assert len(scoped["dataframe"]) <= len(everything["dataframe"])
    if not scoped["dataframe"].empty:
        assert scoped["dataframe"]["host"].str.contains("192.168.1.74").all()


def test_audit_finds_changes_for_a_known_host():
    """Returned 0 for every host before the resource-id rewrite."""
    from tools.zabbix_tool import query_zabbix
    result = query_zabbix.invoke({"query_type": "audit", "host_filter": "192.168.1.80"})
    assert len(result["dataframe"]) > 0, "audit lookup regressed to finding nothing"
    assert "Configuration changes recorded" in str(result["summary"])


def test_zammad_summary_window_matches_the_filter():
    """Summary said 130 opened in 10 days while the filter returned 127."""
    from tools.zammad_tool import query_zammad
    created = query_zammad.invoke({"date_filter": "last 10 days", "date_field": "created"})
    stated = _number_after(str(created["summary"]), "Opened in last 10 days:")
    assert stated == len(created["dataframe"]), (
        f"summary says {stated}, filter returns {len(created['dataframe'])}")


def test_zammad_closed_window_matches_the_filter():
    from tools.zammad_tool import query_zammad
    closed = query_zammad.invoke({"date_filter": "last 10 days", "date_field": "closed"})
    stated = _number_after(str(closed["summary"]), "Closed in last 10 days:")
    assert stated == len(closed["dataframe"])


def test_zammad_named_state_does_not_over_expand():
    """'Open State' must mean the state named 'open', not every active state."""
    from tools.zammad_tool import query_zammad
    result = query_zammad.invoke({"state_filter": "open"})
    df = result["dataframe"]
    assert not df.empty
    assert set(df["state_name"].str.lower()) == {"open"}


def test_zammad_paging_reaches_the_whole_estate():
    """The old fixed 44-page cap was six pages from silently dropping tickets."""
    from tools.clients import ZammadClient
    tickets = ZammadClient().all_tickets()
    assert len(tickets) > 100
    assert len({t["id"] for t in tickets}) == len(tickets), "duplicate tickets across pages"


def test_prometheus_vms_resolve_to_names_not_domain_ids():
    from tools.prometheus_tool import query_prometheus
    df = query_prometheus.invoke({"metric": "vms"})["dataframe"]
    assert not df.empty
    assert not df["vm"].str.startswith("instance-0000").any(), (
        "VM identity join broke; falling back to raw libvirt domain ids")


def test_prometheus_vm_cpu_is_normalised_per_vcpu():
    """libvirt sums CPU across vCPUs; a 16-vCPU VM read as 598% before."""
    from tools.prometheus_tool import query_prometheus
    df = query_prometheus.invoke({"metric": "vm_utilization"})["dataframe"]
    assert not df.empty
    assert df["cpu_percent"].max() <= 150, (
        f"cpu_percent of {df['cpu_percent'].max()} suggests vCPU normalisation regressed")
    assert (df["vcpus"] > 0).all()


def test_prometheus_host_filter_builds_valid_promql():
    """re.escape produced '\\.' which PromQL rejects with a 400."""
    from tools.prometheus_tool import query_prometheus
    for host in ("server01", "server02", "192.168.1.29"):
        summary = str(query_prometheus.invoke({"metric": "memory", "host_filter": host})["summary"])
        assert "failed" not in summary.lower(), f"{host}: {summary[:140]}"
        assert "Memory used %" in summary


def test_prometheus_rejects_promql_injection():
    from tools.prometheus_tool import query_prometheus
    result = query_prometheus.invoke({"metric": "cpu", "host_filter": 'x") or vector(1) #'})
    promql = str(result["raw"].get("promql", ""))
    assert '"' not in promql.split('instance=~"')[-1].split('"')[0]
    assert "vector(1)" not in promql
