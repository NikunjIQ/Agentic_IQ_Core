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
                            HONESTY_EXPECTATIONS, ZABBIX_SAMPLE_QUESTIONS)


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


def test_every_original_sample_question_still_has_an_answering_query():
    """The 24 questions the Zabbix tool was originally specified against.

    The tool was built from this list, and then the list outgrew it — uptime,
    SLA, IOS version and EOL/EOS were all asked from the start and none of them
    was answerable. Keeping the mapping checkable means a future restructure
    cannot quietly drop coverage the way growing the tool quietly failed to
    add it.
    """
    covered = {label for label, _, _ in ZABBIX_QUERIES}
    missing = [(question, label) for question, label in ZABBIX_SAMPLE_QUESTIONS
               if label not in covered]
    assert not missing, f"sample questions with no answering query: {missing}"
    assert len(ZABBIX_SAMPLE_QUESTIONS) == 24, "the original corpus was 24 questions"


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


def test_uptime_is_found_under_both_key_families():
    """SNMP kit reports system.net.uptime; agent hosts report system.uptime.

    Searching only one key answered for half the estate — a router would have
    said "no items found" while a Linux VM answered fine.
    """
    from tools.zabbix_tool import query_zabbix
    snmp = query_zabbix.invoke({"query_type": "items", "host_filter": "192.168.1.73",
                                "item_keyword": "uptime"})
    agent = query_zabbix.invoke({"query_type": "items", "host_filter": "Nikunj VM",
                                 "item_keyword": "uptime"})
    for label, result in (("SNMP host", snmp), ("agent host", agent)):
        assert "Uptime" in result["summary"], f"{label}: {result['summary'][:200]}"
        assert not result["dataframe"].empty, f"{label} returned no uptime rows"
        # Rendered as a duration, not as a bare seconds count.
        assert "d" in str(result["dataframe"]["display_value"].iloc[0])


def test_a_metric_can_be_read_across_a_whole_group():
    """"What is the uptime of the routers" is one question, not one per device.

    The items branch used to require a single host_filter and ignore
    group_filter entirely, so a question about a class of device could only be
    answered by naming each host. The model routed such questions to
    query_type='hosts' instead, which reports up/down and cannot report a metric
    at all — the answer said "2 routers, both up, go and check their uptime
    in Zabbix".
    """
    from tools.zabbix_tool import query_zabbix
    result = query_zabbix.invoke({"query_type": "items", "group_filter": "Routers",
                                  "item_keyword": "uptime"})
    summary = result["summary"]
    df = result["dataframe"]
    assert "across 2 host(s)" in summary, summary[:200]
    assert set(df["host"]) == {"192.168.1.74", "192.168.1.75"}, set(df["host"])
    # Every device reports its own reading, not one shared figure.
    assert df["display_value"].nunique() == 2, df[["host", "display_value"]].to_dict()


def test_a_group_name_given_as_host_filter_still_resolves():
    """People ask for "the uptime for switches"; 'switches' is a group, not a host.

    Without the fallback this answered "could not find a host matching
    'switches'" and stopped.
    """
    from tools.zabbix_tool import query_zabbix
    result = query_zabbix.invoke({"query_type": "items", "host_filter": "switches",
                                  "item_keyword": "uptime"})
    assert "could not find" not in result["summary"].lower(), result["summary"][:200]
    assert "Uptime" in result["summary"]
    assert not result["dataframe"].empty


def test_an_unknown_scope_is_named_as_neither_host_nor_group():
    from tools.zabbix_tool import query_zabbix
    summary = query_zabbix.invoke({"query_type": "items", "host_filter": "toasters",
                                   "item_keyword": "uptime"})["summary"]
    assert "host or host group" in summary, summary[:200]


def test_uptime_of_zero_is_reported_as_not_collected():
    """A running device never has zero uptime, so 0 means nothing came back.

    'Windows' has a configured uptime item and an agent that is not responding.
    Passing the 0 through would claim the host had just been rebooted.
    """
    from tools.zabbix_tool import query_zabbix
    result = query_zabbix.invoke({"query_type": "items", "host_filter": "Windows",
                                  "item_keyword": "uptime"})
    assert "no reading collected" in result["summary"], result["summary"][:200]
    assert "0d" not in result["summary"]


def test_version_and_hardware_readings_are_not_sorted_numerically():
    """Version strings have no ordering, and _numeric sinks them all to -inf."""
    from tools.zabbix_tool import query_zabbix
    result = query_zabbix.invoke({"query_type": "items", "host_filter": "192.168.1.75",
                                  "item_keyword": "IOS version and model"})
    summary = result["summary"]
    assert "OS / software version" in summary and "Hardware" in summary, summary[:300]
    assert "highest first" not in summary, "text readings must not claim an ordering"


def test_long_device_descriptions_do_not_swamp_the_summary():
    """Cisco sysDescr is a multi-line block with a support URL and a copyright.

    Passed through whole it crowded out every other metric in the summary the
    model reads.
    """
    from tools.zabbix_tool import query_zabbix
    result = query_zabbix.invoke({"query_type": "items", "host_filter": "192.168.1.73",
                                  "item_keyword": "IOS version"})
    assert "\n" not in result["summary"], "raw newlines leaked into the summary"
    for value in result["dataframe"]["display_value"]:
        assert len(str(value)) <= 140, f"untrimmed reading: {str(value)[:80]}"


def test_eol_says_zabbix_does_not_hold_it_and_offers_the_identifiers():
    """Zabbix models no equipment lifecycle. An empty table would read as "fine"."""
    from tools.zabbix_tool import query_zabbix
    result = query_zabbix.invoke({"query_type": "items", "host_filter": "192.168.1.75",
                                  "item_keyword": "EOL EOS"})
    summary = result["summary"]
    assert "does not record end-of-life" in summary, summary[:300]
    # The model and serial an external lookup would need come back with it.
    assert "Hardware (model / serial)" in summary, summary[:300]


def test_metric_keywords_do_not_fire_on_substrings():
    """'os' sits inside 'hosts' and 'ram' inside 'program'."""
    from tools.zabbix_tool import requested_metrics
    for text in ("hosts", "how many devices are up", "program"):
        assert requested_metrics(text) == [], f"{text!r} matched a metric"
    # ...while the real keywords still resolve.
    assert [m["label"] for m in requested_metrics("cpu & memory")] == ["CPU", "Memory"]


def test_username_password_auth_works_alongside_the_token():
    """The user/pass fallback was broken twice over and nothing noticed.

    It is dead code whenever ZABBIX_API_TOKEN is set, which is how it stayed
    broken: user.login's "user" parameter became "username" in 6.0 and was
    removed in 6.4, and 6.4 also dropped the payload "auth" field in favour of
    the Authorization header. Anyone configuring username and password through
    the sidebar instead of a token hit both.
    """
    import os
    from tools.zabbix.client import build_rpc

    if not (os.getenv("ZABBIX_USER") and os.getenv("ZABBIX_PASS")):
        return   # nothing to check on an installation that only has a token

    original = os.environ.get("ZABBIX_API_TOKEN", "")
    os.environ["ZABBIX_API_TOKEN"] = ""
    try:
        count = build_rpc()("host.get", {"countOutput": True})
    finally:
        os.environ["ZABBIX_API_TOKEN"] = original
    assert int(count) > 0, "user/pass auth returned no hosts"


def test_sla_reports_target_and_achieved_figure():
    """'What is the SLA for devices' — answerable since the Services branch."""
    from tools.zabbix_tool import query_zabbix
    result = query_zabbix.invoke({"query_type": "sla"})
    summary = result["summary"]
    assert "SLA(s) configured" in summary, summary[:200]
    df = result["dataframe"]
    assert not df.empty and {"slo_target_pct", "current_sli_pct"} <= set(df.columns)
    # A target without an achieved figure is half an answer.
    assert df["slo_target_pct"].notna().any()
    assert df["current_sli_pct"].notna().any()


def test_sla_unmeasured_periods_are_not_scored_as_breaches():
    """Zabbix returns sli = -1 for periods before the SLA's effective date.

    It is a "not measured" sentinel, not a percentage. Compared against the
    target it made a service that has never been down report as breaching.
    """
    from tools.zabbix_tool import query_zabbix
    df = query_zabbix.invoke({"query_type": "sla"})["dataframe"]
    measured = df[df["current_sli_pct"].notna()]
    assert not measured.empty
    assert (measured["current_sli_pct"] >= 0).all(), "a sentinel leaked in as a percentage"
    for _, row in measured.iterrows():
        if row["current_sli_pct"] >= row["slo_target_pct"] and row["downtime_s"] == 0:
            assert row["periods_below_target"] == 0, (
                f"{row['service']} has no downtime and meets target, but "
                f"{row['periods_below_target']} period(s) were scored as breaches")


def test_sla_resolves_a_device_name_to_its_ip_tagged_service():
    """Services are tagged by IP; people ask by name.

    'Nikunj VM' is 192.168.1.152 and 'Windows' is 192.168.1.104 — without
    device resolution both questions return nothing.
    """
    from tools.zabbix_tool import query_zabbix
    for name, expected_ip in (("Nikunj VM", "192.168.1.152"), ("Windows", "192.168.1.104")):
        df = query_zabbix.invoke({"query_type": "sla", "host_filter": name})["dataframe"]
        assert not df.empty, f"{name} resolved to no service"
        assert set(df["service"]) == {expected_ip}, f"{name} -> {set(df['service'])}"


def test_sla_says_when_a_device_has_no_service_level_defined():
    """No SLA coverage is a configuration gap, not a healthy result."""
    from tools.zabbix_tool import query_zabbix
    result = query_zabbix.invoke({"query_type": "sla", "host_filter": "192.168.1.80"})
    summary = result["summary"]
    assert "none of them covers" in summary, summary[:250]
    assert "configuration gap" in summary


def test_sla_names_services_that_no_sla_measures():
    """"Meeting its target" and "never measured" must not look alike."""
    from tools.zabbix_tool import query_zabbix
    summary = query_zabbix.invoke({"query_type": "sla"})["summary"]
    assert "not covered by any SLA" in summary, summary[-400:]


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


def test_zammad_tag_filter_uses_the_tag_taxonomy():
    """Tags are a real taxonomy here, and were unusable as a filter.

    110 tags with structured prefixes — class:, component:, scope:, target:,
    host:. 'host: 192.168.1.73' is an exact device-to-ticket key, better than
    parsing it out of the ticket body.
    """
    from tools.zammad_tool import query_zammad
    result = query_zammad.invoke({"tag_filter": "host: 192.168.1.73"})
    assert "tags include" in result["summary"], result["summary"][-300:]
    assert not result["dataframe"].empty


def test_zammad_search_cap_is_reported_as_a_minimum():
    """Zammad's search returns at most 200 rows whatever limit is asked for.

    200, 500, 1000 and 2000 all come back with 200, so a tag with more tickets
    than that reads as exactly 200 unless the ceiling is stated. 'class: os' is
    on 2,270 tickets.
    """
    from tools.zammad_tool import query_zammad
    summary = query_zammad.invoke({"tag_filter": "class: os"})["summary"]
    assert "MINIMUM, not a total" in summary, summary[-400:]
    # The authoritative figure from the tag index comes back with it.
    assert "tag index counts" in summary


def test_zammad_sla_attainment_states_its_coverage():
    """"96% within SLA" is measured on 24 of 3,825 tickets.

    The figure is Zammad's own, not recomputed, but without the coverage it
    reads as an estate-wide health statement and is not one.
    """
    from tools.zammad_tool import query_zammad
    summary = query_zammad.invoke({})["summary"]
    assert "SLA policies configured" in summary, "SLA targets missing"
    if "First response SLA" in summary:
        assert "of the estate" in summary, "SLA attainment given without its coverage"


def test_zammad_named_queue_resolves_to_zammad_own_view():
    """Overview conditions are arbitrary; the server evaluates its own view."""
    from tools.zammad_tool import query_zammad
    result = query_zammad.invoke({"overview": "Escalated Tickets"})
    assert "in queue 'Escalated Tickets'" in result["summary"], result["summary"][-250:]
    bad = query_zammad.invoke({"overview": "Nonexistent Queue"})
    assert "No Zammad queue named" in bad["summary"]
    assert "Available:" in bad["summary"], "an unknown queue should name the real ones"


def test_zammad_ticket_conversation_is_reachable():
    """Articles carry the Zabbix problem detail and any human replies."""
    from tools.zammad_tool import query_zammad
    result = query_zammad.invoke({"ticket_reference": "904298"})
    summary = result["summary"]
    assert "article(s)" in summary, summary[:200]
    assert "Tags:" in summary
    assert not result["dataframe"].empty
    # HTML email bodies must not reach the summary as markup.
    assert "<div>" not in summary
    missing = query_zammad.invoke({"ticket_reference": "999999"})
    assert "No ticket found" in missing["summary"]


def test_ceph_answers_every_expected_question():
    from golden_queries import CEPH_QUERIES
    from tools.ceph_tool import query_ceph
    for label, args, must_contain in CEPH_QUERIES:
        result = query_ceph.invoke(dict(args))
        summary = str(result["summary"])
        assert "error" not in summary[:20].lower(), f"{label}: {summary[:140]}"
        assert must_contain in summary, f"{label}: expected {must_contain!r} in {summary[:180]!r}"


def test_ceph_image_listing_is_not_silently_paginated():
    """/api/block/image returns a short page by default.

    The default gave 5 images of 37 and omitted the `volumes` pool entirely —
    the one holding the Cinder data — so a caller trusting it would report that
    Cinder volumes do not exist. The client passes limit=-1.
    """
    from tools.ceph_tool import query_ceph
    result = query_ceph.invoke({"query_type": "images"})
    pools = set(result["dataframe"]["pool"])
    assert "volumes" in pools, f"the volumes pool is missing — pagination is back: {pools}"
    assert len(result["dataframe"]) > 5, "only a first page of images was returned"


def test_ceph_pool_stats_are_requested():
    """/api/pool reports every pool at zero bytes without stats=true."""
    from tools.ceph_tool import query_ceph
    df = query_ceph.invoke({"query_type": "pools"})["dataframe"]
    assert df["raw_used_GB"].sum() > 0, (
        "every pool reports zero — stats=true is missing, and the cluster reads as empty")


def test_ceph_reports_usable_not_just_raw_capacity():
    """Raw free overstates what can be stored by the replication factor."""
    from tools.ceph_tool import query_ceph
    result = query_ceph.invoke({"query_type": "capacity"})
    summary = result["summary"]
    assert "raw free" in summary and "usable free space is lower" in summary, summary[:250]
    usable = result["dataframe"]["usable_free_TB"].max()
    raw = float(summary.split("TB raw free")[0].split(",")[-1].strip().split()[0])
    assert usable < raw, f"usable ({usable}) should be below raw ({raw})"


def test_ceph_does_not_answer_vm_counts():
    """Ceph sees only VMs whose disk is in its pool; six of seven are invisible."""
    from tools.ceph_tool import query_ceph
    summary = query_ceph.invoke({"query_type": "images"})["summary"]
    assert "use query_prometheus" in summary, summary[-300:]


def test_ceph_health_ok_still_reports_firing_alerts():
    """HEALTH_OK and a firing alert are two different truths."""
    from tools.ceph_tool import query_ceph
    result = query_ceph.invoke({"query_type": "health"})
    summary = result["summary"]
    assert "Ceph cluster status" in summary
    if not result["dataframe"].empty and "HEALTH_OK" in summary:
        assert "while these alerts are firing" in summary, (
            "alerts are firing but the answer presents HEALTH_OK unqualified")


def test_zammad_text_search_finds_a_real_term():
    """Without free text, "is there a ticket about X" could not be answered."""
    from tools.zammad_tool import query_zammad
    result = query_zammad.invoke({"search": "Zabbix agent"})
    assert "text ~ 'Zabbix agent'" in result["summary"], result["summary"][-300:]
    assert not result["dataframe"].empty


def test_zammad_absent_subject_says_why_it_is_absent():
    """No ticket mentions ceph, and the reason matters.

    Only Zabbix raises tickets automatically. Ceph and Prometheus have no ticket
    integration, so nothing from them could ever appear — a genuine absence, not
    a gap in the search.
    """
    from tools.zammad_tool import query_zammad
    result = query_zammad.invoke({"search": "ceph"})
    summary = result["summary"]
    assert "0 ticket(s) match [text ~ 'ceph']" in summary, summary[-300:]
    assert "no ticket mentions" in summary
    assert "Zabbix" in summary and "no ticket integration" in summary


def test_zammad_text_search_cap_is_reported():
    """Search stops at 200 rows whatever limit asks for."""
    from tools.zammad_tool import query_zammad
    summary = query_zammad.invoke({"search": "interface"})["summary"]
    if "200 ticket(s) match" in summary:
        assert "MINIMUM" in summary, "a capped count is being presented as a total"


def test_every_official_ceph_question_has_an_answering_query():
    """The 19 questions Ceph is expected to answer, mapped to a real query."""
    from golden_queries import CEPH_QUERIES, CEPH_SAMPLE_QUESTIONS
    covered = {label for label, _, _ in CEPH_QUERIES}
    missing = [(q, label) for q, label in CEPH_SAMPLE_QUESTIONS if label not in covered]
    assert not missing, f"official questions with no answering query: {missing}"
    assert len(CEPH_SAMPLE_QUESTIONS) == 19, "the official corpus is 19 questions"


def test_ceph_endpoints_never_return_a_credential():
    """/api/settings returns GRAFANA_API_PASSWORD in plaintext.

    Answering "what is the Grafana URL" by handing that endpoint's output to a
    model would put the password into the prompt and into the chat transcript.
    The client whitelists specific keys rather than filtering on the way out.
    """
    from tools.ceph_tool import query_ceph
    result = query_ceph.invoke({"query_type": "endpoints"})
    blob = str(result["summary"]) + str(result["raw"])
    for forbidden in ("PASSWORD", "SECRET", "ACCESS_KEY", "API_KEY"):
        assert forbidden not in blob.upper(), f"{forbidden} leaked into a Ceph answer"
    assert "GRAFANA_API_URL" in blob, "the URL that was actually asked for is missing"


def test_ceph_crush_returns_the_hierarchy_a_treemap_needs():
    """The treemap is drawn by the frontend from root -> host -> OSD rows."""
    from tools.ceph_tool import query_ceph
    df = query_ceph.invoke({"query_type": "crush"})["dataframe"]
    assert not df.empty
    for column in ("root", "host", "osd", "crush_weight", "size_GB"):
        assert column in df.columns, f"{column} missing — the treemap cannot be drawn"
    assert df["host"].nunique() >= 1 and len(df) >= df["host"].nunique()


def test_ceph_logs_flag_concerning_entries_separately():
    """General log AND the concerning subset; an absence of warnings is an answer."""
    from tools.ceph_tool import query_ceph
    result = query_ceph.invoke({"query_type": "logs"})
    df, summary = result["dataframe"], result["summary"]
    assert {"priority", "concerning", "disk_related"} <= set(df.columns)
    assert "cluster log entr(ies)" in summary
    if not df["concerning"].any():
        assert "no disk failures" in summary.lower(), (
            "no warnings were found and the answer does not say so")


def test_ceph_snapshots_report_when_the_last_one_was_taken():
    from tools.ceph_tool import query_ceph
    result = query_ceph.invoke({"query_type": "snapshots"})
    assert "Most recent snapshot taken" in result["summary"]
    assert "taken_at" in result["dataframe"].columns


def test_ceph_hosts_carry_every_field_the_api_returned():
    """A hand-picked projection made the model report Ceph as lacking the data.

    /api/host returns eight fields per host. The table was built from a fixed
    list of three, so the address never reached the answer and the model said
    "no IP address is included in that payload" — a confident claim about Ceph
    that was actually a claim about our own column list. Rows are now built
    from the payload's own keys, so a field cannot go missing silently.
    """
    from tools.ceph_tool import query_ceph
    result = query_ceph.invoke({"query_type": "hosts"})
    payload = result["raw"]["hosts"]
    assert payload, "no Ceph hosts returned"

    columns = set(result["dataframe"].columns)
    for key in payload[0]:
        assert key in columns, f"{key} was dropped from the table"

    # The address is a join key to Prometheus and Zabbix, so it is stated as a
    # fact in the summary rather than left in a table the model samples.
    summary = str(result["summary"])
    for host in payload:
        if host.get("addr"):
            assert host["addr"] in summary, f"{host['addr']} missing from summary"

    # Which daemons run where is only in service_instances; it was dropped too.
    assert "mgr" in summary and "mon" in summary, summary[:200]


def test_ceph_wide_payloads_name_the_fields_they_do_not_show():
    """Trimming is fine; trimming silently is not.

    /api/pool returns 64 fields per pool. Showing them all runs to ~68,000
    characters against an 8,000-token-per-minute budget, so the table stays
    narrow — but a model shown seven columns reported the rest as absent from
    Ceph, which is a claim about our projection wearing the cluster's clothes.
    Each wide payload now names what it left out.
    """
    from tools.ceph_tool import query_ceph
    for query_type, expect in (("pools", "pg_num"),
                               ("osds", "weight"),
                               ("images", "obj_size")):
        summary = str(query_ceph.invoke({"query_type": query_type})["summary"])
        assert "not missing from Ceph" in summary, f"{query_type}: no field note"
        assert expect in summary, f"{query_type}: expected {expect!r} to be named"

    # Narrow payloads have nothing to disclose, so they must stay quiet rather
    # than padding every answer with a note.
    for query_type in ("hosts", "snapshots"):
        summary = str(query_ceph.invoke({"query_type": query_type})["summary"])
        assert "Also available:" not in summary, f"{query_type}: spurious field note"


def test_ceph_logs_say_which_daemon_logged_the_entry():
    """`name` (mon.server01) was dropped, so "where is this from" was unanswerable."""
    from tools.ceph_tool import query_ceph
    result = query_ceph.invoke({"query_type": "logs"})
    assert "daemon" in result["dataframe"].columns
    assert result["dataframe"]["daemon"].str.contains(r"\.", regex=True).any()


# --- ceph <-> prometheus VM correlation --------------------------------------
def test_vm_storage_correlation_matches_the_known_ceph_backed_vm():
    """Live-verified against the real environment before this tool was written:
    Ceph's vms-pool image name 1a05a978-3dd7-488a-8801-d234565a8ed6_disk and
    Prometheus's instance_id=1a05a978-... on libvirt_domain_openstack_info name
    the same VM, jammy. Turning that fact into a permanent check the same way
    every other shipped bug in this codebase became one.
    """
    from tools.vm_storage_correlation import correlate_vm_storage_and_compute
    result = correlate_vm_storage_and_compute.invoke({"vm_filter": None})
    df = result["dataframe"]
    assert not df.empty, "no VMs returned at all"

    jammy = df[df["vm"] == "jammy"]
    assert not jammy.empty, "jammy must appear in the joined VM list"
    row = jammy.iloc[0]
    assert bool(row["on_ceph"]), "jammy is the one VM known to have a Ceph-backed disk"
    assert row["instance_id"] == "1a05a978-3dd7-488a-8801-d234565a8ed6"
    assert row["ceph_provisioned_GB"] > 0

    # Most VMs are on local hypervisor disk and must be reported as such, not
    # silently dropped — the absence is usually the answer for this tool.
    others = df[df["vm"] != "jammy"]
    assert not others.empty
    assert not others["on_ceph"].any(), "only jammy is expected to be Ceph-backed today"
    assert "jammy" in result["summary"]


def test_vm_storage_correlation_filter_narrows_to_one_vm():
    from tools.vm_storage_correlation import correlate_vm_storage_and_compute
    result = correlate_vm_storage_and_compute.invoke({"vm_filter": "jammy"})
    df = result["dataframe"]
    assert len(df) == 1
    assert df.iloc[0]["vm"] == "jammy"
