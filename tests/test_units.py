"""Offline tests: wiring, the answer cache, and conversation history.

No network, and no LLM except where the `llm` marker says otherwise, so these
run in milliseconds and cost nothing. They are the tests that should never be
skipped.
"""

import time

import pytest


# --- wiring -----------------------------------------------------------------
def test_all_tools_are_registered():
    from agent.tool_registry import TOOL_REGISTRY
    expected = {"query_zabbix", "query_zammad", "query_prometheus", "query_opensearch",
                "correlate_problems_and_tickets", "lookup_ticket_context"}
    assert expected <= set(TOOL_REGISTRY), f"missing: {expected - set(TOOL_REGISTRY)}"


def test_log_level_aliases_have_no_overlap():
    """One spelling must map to exactly one severity, or counts double-count."""
    from tools.clients import LOG_LEVEL_ALIASES
    seen = {}
    for canonical, variants in LOG_LEVEL_ALIASES.items():
        for variant in variants:
            assert variant not in seen, f"{variant!r} maps to both {seen[variant]} and {canonical}"
            seen[variant] = canonical


def test_log_level_canonicalisation():
    from tools.clients import canonical_level
    assert canonical_level("ERROR") == canonical_level("error") == "error"
    assert canonical_level("WARN") == canonical_level("Warning") == "warning"
    assert canonical_level("EMER") == "critical"


def test_opensearch_date_parsing_without_network():
    from tools.opensearch_tool import _time_range
    today, today_label = _time_range("today")
    assert today["gte"] == "now/d" and "today" in today_label
    yesterday, _ = _time_range("yesterday")
    assert yesterday == {"gte": "now-1d/d", "lt": "now/d"}
    hours, label = _time_range("last 6 hours")
    assert hours["gte"] == "now-6h" and "6" in label
    exact, label = _time_range("2026-08-11")
    assert exact["gte"].startswith("2026-08-11") and "2026-08-11" in label
    default, _ = _time_range(None)
    assert default["gte"] == "now/d", "no window given must default to today, not all time"


def test_every_tool_exposes_a_usable_description():
    from agent.tool_registry import TOOL_REGISTRY
    for name, entry in TOOL_REGISTRY.items():
        description = entry["description"] or ""
        # The docstring IS the prompt the model routes on; a thin one silently
        # degrades tool selection.
        assert len(description) > 200, f"{name} description is too short ({len(description)})"


def test_tool_schemas_build_for_every_tool():
    from agent.tool_schemas import TOOL_SCHEMAS
    from agent.tool_registry import TOOL_REGISTRY
    assert len(TOOL_SCHEMAS) == len(TOOL_REGISTRY)
    for schema in TOOL_SCHEMAS:
        assert schema["type"] == "function"
        assert schema["function"].get("name")
        assert schema["function"].get("parameters")


def test_routing_hints_survive_in_the_docstrings():
    """These sentences are load-bearing — each one fixed a real misroute."""
    from agent.tool_registry import TOOL_REGISTRY
    zabbix = TOOL_REGISTRY["query_zabbix"]["description"]
    assert "problems" in zabbix and "alerts" in zabbix
    zammad = TOOL_REGISTRY["query_zammad"]["description"]
    assert "ONLY set this when the user names a time period" in zammad, \
        "date_filter guard missing; the 8B invents time windows without it"
    # The trigger was a concrete value sitting in the date_filter PARAMETER
    # description — the model reached for it when the question named no period.
    # Elsewhere in the docstring the phrase is fine: it describes what the summary
    # reports, which is how a genuine 10-day question gets answered without a filter.
    date_section = zammad.split("- date_filter:")[1].split("\n    - ")[0]
    assert "last 10 days" not in date_section, \
        "a concrete date example is back in the date_filter description; it primes the guess"
    assert "exact state only" in zammad, "named-state guard missing; 'Open State' over-expands"
    prom = TOOL_REGISTRY["query_prometheus"]["description"]
    assert "NOT physical" in prom or "NOT the physical" in prom or "virtual routers" in prom.lower()


def test_backend_app_imports():
    import main
    assert main.app is not None


# --- conversation history ---------------------------------------------------
def test_history_returns_only_recent_turns():
    from agent.history import recent_turns
    history = [{"role": "user", "content": f"q{i}"} for i in range(20)]
    assert len(recent_turns(history, max_turns=6)) == 6


def test_history_truncates_long_answers():
    from agent.history import recent_turns, MAX_CHARS_PER_TURN
    history = [{"role": "assistant", "content": "x" * 9000}]
    turn = recent_turns(history)[0]
    assert len(turn["content"]) < MAX_CHARS_PER_TURN + 50


def test_history_ignores_malformed_entries():
    from agent.history import recent_turns
    history = [None, "not a dict", {"role": "system", "content": "x"},
               {"role": "user"}, {"role": "user", "content": "real"}]
    turns = recent_turns(history)
    assert turns == [{"role": "user", "content": "real"}]


def test_history_handles_empty():
    from agent.history import recent_turns
    assert recent_turns(None) == []
    assert recent_turns([]) == []


def test_nodes_actually_pass_history():
    """Guards the bug this fixed: the simple path ignored history entirely."""
    import inspect
    from agent.nodes import simple_path, classify
    for module in (simple_path, classify):
        source = inspect.getsource(module)
        assert "recent_turns" in source, f"{module.__name__} dropped conversation history"


# --- answer cache -----------------------------------------------------------
def test_cache_stores_and_returns_an_answer():
    from agent import answer_cache
    answer_cache.clear()
    payload = {"final_answer": "3 hosts are down", "complexity": "simple",
               "tools_called": ["query_zabbix"], "dataframes": [], "tool_results_raw": []}
    answer_cache.put("how many hosts are down", payload, [])
    hit = answer_cache.get("how many hosts are down", [])
    assert hit is not None and hit["final_answer"] == "3 hosts are down"
    assert hit["cached"] is True


def test_cache_ignores_wording_noise():
    from agent import answer_cache
    answer_cache.clear()
    answer_cache.put("How many hosts are DOWN?", {"final_answer": "3", "complexity": "simple"}, [])
    assert answer_cache.get("how many   hosts are down", []) is not None


def test_cache_separates_questions_by_history():
    """'what about server02?' means different things in different conversations."""
    from agent import answer_cache
    answer_cache.clear()
    first = [{"role": "user", "content": "cpu of server01"}]
    second = [{"role": "user", "content": "ticket count"}]
    answer_cache.put("what about server02", {"final_answer": "cpu answer",
                                             "complexity": "simple"}, first)
    assert answer_cache.get("what about server02", first) is not None
    assert answer_cache.get("what about server02", second) is None


def test_cache_never_stores_errors():
    from agent import answer_cache
    answer_cache.clear()
    answer_cache.put("q", {"final_answer": "rate limit reached", "complexity": "error"}, [])
    assert answer_cache.get("q", []) is None


def test_cache_never_stores_empty_answers():
    from agent import answer_cache
    answer_cache.clear()
    answer_cache.put("q", {"final_answer": "  ", "complexity": "simple"}, [])
    assert answer_cache.get("q", []) is None


def test_cache_expires():
    from agent import answer_cache
    answer_cache.clear()
    answer_cache.put("q", {"final_answer": "a", "complexity": "simple"}, [])
    assert answer_cache.get("q", [], ttl=100) is not None
    time.sleep(1.1)
    assert answer_cache.get("q", [], ttl=1) is None


def test_cache_bypassed_when_freshness_is_requested():
    from agent import answer_cache
    answer_cache.clear()
    for phrasing in ("check the hosts again", "refresh the host list",
                     "what is down right now"):
        answer_cache.put(phrasing, {"final_answer": "stale", "complexity": "simple"}, [])
        assert answer_cache.get(phrasing, []) is None, f"{phrasing!r} should bypass the cache"


def test_cache_evicts_rather_than_growing_forever():
    from agent import answer_cache
    answer_cache.clear()
    for i in range(answer_cache.MAX_ENTRIES + 20):
        answer_cache.put(f"question {i}", {"final_answer": str(i),
                                           "complexity": "simple"}, [])
    assert answer_cache.stats()["entries"] <= answer_cache.MAX_ENTRIES


# --- saying what was not seen -----------------------------------------------
def test_truncated_table_says_how_many_rows_were_hidden():
    """The simple path used to say only 'First 10 rows', so the model could not
    know it was reading part of a table and answered as if it had seen it all."""
    import pandas as pd
    from agent.coverage import describe_table
    header = describe_table(pd.DataFrame({"a": range(48)})).strip().splitlines()[0]
    assert "10 of 48" in header
    assert "do NOT count" in header


def test_untruncated_table_says_it_is_complete():
    import pandas as pd
    from agent.coverage import describe_table
    assert "all 4 rows" in describe_table(pd.DataFrame({"a": range(4)}))
    assert describe_table(pd.DataFrame()) == ""
    assert describe_table(None) == ""


def test_empty_opensearch_result_about_a_router_is_explained():
    """Zero there means 'not collected', never 'the network is healthy'."""
    from agent.coverage import empty_result_note
    note = empty_result_note("query_opensearch",
                             "show me logs for router 192.168.1.73", "No logs found")
    assert "not collected" in note.lower()
    assert "zabbix" in note.lower()


def test_a_genuine_zero_gets_no_spurious_explanation():
    from agent.coverage import empty_result_note
    assert empty_result_note("query_opensearch", "keystone errors today", "No logs") == ""
    assert empty_result_note("query_zabbix", "how many hosts are down", "0 hosts") == ""


def test_partial_coverage_named_only_for_broad_questions():
    from agent.coverage import coverage_note
    broad = coverage_note(["query_zabbix"], "is everything ok")
    assert "Not checked" in broad and "Prometheus" in broad
    assert coverage_note(["query_zabbix"], "how many hosts are down") == ""
    assert coverage_note([], "is everything ok") == ""


def test_empty_detection_handles_missing_pieces():
    import pandas as pd
    from agent.coverage import looks_empty
    assert looks_empty({}) is True
    assert looks_empty({"dataframe": pd.DataFrame()}) is True
    assert looks_empty({"dataframe": pd.DataFrame({"a": [1]})}) is False
    assert looks_empty({"raw": {"hosts": [1, 2]}}) is False


def test_request_metadata_is_not_mistaken_for_results():
    """raw always carries the query that produced it; counting that as data made
    every empty OpenSearch result look populated."""
    import pandas as pd
    from agent.coverage import looks_empty
    opensearch_empty = {"dataframe": pd.DataFrame(),
                        "raw": {"query": {"bool": {"filter": [1, 2]}}, "total": 0}}
    assert looks_empty(opensearch_empty) is True
    prometheus_empty = {"dataframe": pd.DataFrame(),
                        "raw": {"promql": "up{job='x'}", "rows": []}}
    assert looks_empty(prometheus_empty) is True
    assert looks_empty({"dataframe": pd.DataFrame(),
                        "raw": {"query": {"x": 1}, "total": 12}}) is False


def test_both_paths_report_what_was_not_seen():
    import inspect
    from agent.nodes import simple_path, complex_path
    for module in (simple_path, complex_path):
        source = inspect.getsource(module)
        assert "coverage.describe_table" in source, \
            f"{module.__name__} does not label truncated tables"
        assert "coverage.looks_empty" in source


# --- follow-up suggestions --------------------------------------------------
def test_suggestions_are_relevant_to_the_tool_that_ran():
    from agent.suggestions import suggest
    assert "ticket" in " ".join(suggest(["query_zabbix"])).lower()
    assert "device" in " ".join(suggest(["query_zammad"])).lower()
    assert suggest(["query_prometheus"]), "no follow-ups after a Prometheus query"


def test_suggestions_are_capped_and_unique():
    from agent.suggestions import suggest, MAX_SUGGESTIONS
    many = suggest(["query_zabbix", "query_zammad", "query_prometheus",
                    "query_opensearch"])
    assert len(many) <= MAX_SUGGESTIONS
    assert len(many) == len(set(many))


def test_no_suggestions_after_a_clarifying_question():
    """The app has just asked something; chips underneath would compete with it."""
    from agent.suggestions import suggest
    assert suggest(["clarify"]) == []
    assert suggest([]) == []
    assert suggest(None) == []


def test_empty_results_suggest_widening_instead():
    from agent.suggestions import suggest
    widen = " ".join(suggest(["query_opensearch"], result_was_empty=True)).lower()
    assert "widen" in widen or "all log levels" in widen


def test_empty_result_still_suggests_something():
    """Not every tool has widening advice — fall back rather than show nothing."""
    from agent.suggestions import suggest
    assert suggest(["lookup_ticket_context"], result_was_empty=True)


def test_suggestions_cost_no_model_call():
    import inspect
    from agent import suggestions
    source = inspect.getsource(suggestions)
    assert "ChatGroq" not in source and "invoke" not in source, \
        "suggestions must stay deterministic — a chip that costs tokens is not free"


def test_suggestions_reach_the_payload():
    import inspect
    import main
    assert "suggestions.suggest" in inspect.getsource(main._serialise_state)


# --- answer style -----------------------------------------------------------
def test_style_names_the_system_actually_queried():
    """The model copied 'e.g. Checked Zabbix' onto a Zammad answer, so the real
    system is supplied as a fact rather than shown as an example."""
    from agent.voice import system_for_tools
    assert system_for_tools("query_zammad") == "Zammad"
    assert system_for_tools("query_prometheus") == "Prometheus"
    assert system_for_tools(["query_zabbix", "query_zammad"]) == "Zabbix and Zammad"
    assert system_for_tools([]) == ""
    assert system_for_tools("not_a_tool") == ""


def test_style_contains_no_copyable_system_example():
    """Three separate bugs came from a concrete example being reproduced verbatim."""
    from agent.voice import CONVERSATIONAL_STYLE, CONVERSATIONAL_STYLE_BRIEF
    for text in (CONVERSATIONAL_STYLE, CONVERSATIONAL_STYLE_BRIEF):
        assert "Checked Zabbix" not in text, "a copyable system name is back in the style"


def test_style_asks_for_the_three_beats():
    from agent.voice import CONVERSATIONAL_STYLE
    lowered = CONVERSATIONAL_STYLE.lower()
    assert "open with" in lowered          # acknowledge
    assert "real values" in lowered        # answer
    assert "next step" in lowered          # suggest
    assert "no pleasantries" in lowered


def test_brief_style_is_actually_brief():
    """It rides on the orchestrator prompt, which is resent every iteration."""
    from agent.voice import CONVERSATIONAL_STYLE, CONVERSATIONAL_STYLE_BRIEF
    assert len(CONVERSATIONAL_STYLE_BRIEF) < len(CONVERSATIONAL_STYLE)
    assert len(CONVERSATIONAL_STYLE_BRIEF) // 4 < 90, "too costly to resend per loop"


def test_both_paths_apply_the_style():
    import inspect
    from agent.nodes import simple_path, complex_path
    assert "CONVERSATIONAL_STYLE" in inspect.getsource(simple_path)
    assert "system_for_tools" in inspect.getsource(simple_path)
    complex_src = inspect.getsource(complex_path)
    # The loop answers most complex questions, so the style must be in the system
    # prompt, not only in the closing call the brief pointed at.
    assert "RULE 4" in complex_src
    assert "system_for_tools" in complex_src


# --- clarifying questions ---------------------------------------------------
def test_clarify_offers_real_host_groups_not_invented_ones():
    """Options must come from the live estate, or the app offers things that
    do not exist and the user picks one that cannot be queried."""
    from agent import clarify
    from agent.metadata import live_options
    groups = live_options().get("zabbix_groups") or []
    if not groups:
        return          # unconfigured environment; covered by the live suite
    question = clarify.build("scope")
    assert any(group in question for group in groups), \
        f"no real host group appears in: {question!r}"


def test_clarify_always_offers_an_escape():
    from agent import clarify
    for axis in ("system", "scope", "timeframe"):
        assert clarify.ESCAPE_OPTION in clarify.build(axis)


def test_clarify_asks_about_the_right_axis():
    from agent import clarify
    assert "time period" in clarify.build("timeframe").lower()
    system = clarify.build("system").lower()
    assert "zabbix" in system and "prometheus" in system


def test_clarify_never_asks_twice_in_a_row():
    """Asking again after the user has answered traps them in a loop."""
    from agent import clarify
    asked = [{"role": "user", "content": "show me what's down"},
             {"role": "assistant", "content": clarify.build("system")}]
    assert clarify.was_just_asked(asked) is True
    normal = [{"role": "user", "content": "how many hosts are down"},
              {"role": "assistant", "content": "There are 3 hosts down."}]
    assert clarify.was_just_asked(normal) is False
    assert clarify.was_just_asked([]) is False


def test_clarify_axis_is_validated():
    from agent import clarify
    assert clarify.normalise_axis("system") == "system"
    assert clarify.normalise_axis("SCOPE") == "scope"
    for junk in ("everything", "", None, "null", 42):
        assert clarify.normalise_axis(junk) == "", f"{junk!r} should not trigger a question"


def test_clarify_costs_no_model_call():
    import inspect
    from agent.nodes import clarify_node
    source = inspect.getsource(clarify_node)
    assert "ChatGroq" not in source and "invoke" not in source, \
        "the clarify node must not call a model — it is meant to be free"


def test_no_rephrase_dead_ends_remain():
    """Every 'please rephrase' has been replaced with a real question."""
    import inspect
    from agent.nodes import simple_path, complex_path
    for module in (simple_path, complex_path):
        source = inspect.getsource(module)
        assert "rephrase it" not in source.lower(), \
            f"{module.__name__} still dead-ends instead of asking"
        assert "clarify.build" in source


def test_graph_routes_vague_questions_to_clarify():
    from agent.graph import route_by_complexity
    assert route_by_complexity({"clarify_axis": "system", "complexity": "simple"}) == "clarify"
    assert route_by_complexity({"clarify_axis": "", "complexity": "simple"}) == "simple_path"
    assert route_by_complexity({"clarify_axis": None, "complexity": "complex"}) == "complex_path"


# --- streaming (no network, no tokens: a fake client stands in) -------------
class _FakeClient:
    def __init__(self, pieces=("Hello ", "world")):
        self.pieces = pieces

    def invoke(self, messages):
        return type("R", (), {"content": "".join(self.pieces)})()

    def stream(self, messages):
        for piece in self.pieces:
            yield type("C", (), {"content": piece})()


def test_streaming_is_off_by_default():
    """/chat must keep working exactly as before — one call, no emitter."""
    from agent import streaming
    streaming.set_emitter(None)
    assert streaming.stream_text(_FakeClient(), []) == "Hello world"
    assert streaming.get_emitter() is None


def test_streaming_emits_tokens_and_still_returns_the_whole_answer():
    from agent import streaming
    emitter = streaming.Emitter()
    streaming.set_emitter(emitter)
    try:
        text = streaming.stream_text(_FakeClient(("The ", "answer ", "is 3.")), [])
        assert text == "The answer is 3.", "streaming must not lose the assembled text"
        tokens = []
        while not emitter.events.empty():
            event = emitter.events.get()
            if isinstance(event, dict) and event["type"] == "token":
                tokens.append(event["text"])
        assert "".join(tokens) == text, "tokens sent must match the text returned"
    finally:
        streaming.set_emitter(None)


def test_status_updates_are_silent_when_not_streaming():
    """A status call outside a streamed request must not raise."""
    from agent import streaming
    streaming.set_emitter(None)
    streaming.status("Querying Zabbix…")


def test_streaming_finish_and_fail_terminate_the_queue():
    from agent import streaming
    emitter = streaming.Emitter()
    emitter.finish({"final_answer": "done"})
    assert emitter.events.get()["type"] == "done"
    assert emitter.events.get() is streaming.DONE

    emitter = streaming.Emitter()
    emitter.fail("rate limited")
    assert emitter.events.get()["type"] == "error"
    assert emitter.events.get() is streaming.DONE


def test_nodes_use_the_streaming_helpers():
    import inspect
    from agent.nodes import simple_path, complex_path
    assert "streaming.stream_text" in inspect.getsource(simple_path)
    complex_src = inspect.getsource(complex_path)
    assert "streaming.stream_with_tools" in complex_src
    assert "streaming.status" in complex_src


# --- argument guard ---------------------------------------------------------
def test_guard_removes_a_time_window_nobody_asked_for():
    """The bug that shipped: 'How many tickets are in Open State?' came back with
    date_filter='last 10 days', so 22 open tickets became 2."""
    from agent.arg_guard import scrub
    args, dropped = scrub(
        {"state_filter": "open", "date_filter": "last 10 days", "date_field": "created"},
        "How many tickets are in Open State?", [])
    assert args == {"state_filter": "open"}
    assert dropped and "date_filter" in dropped[0]


def test_guard_keeps_a_time_window_the_user_asked_for():
    from agent.arg_guard import scrub
    for question in ("How many tickets were closed in the last 10 days?",
                     "show me logs for today",
                     "what happened yesterday?",
                     "tickets created this month",
                     "errors in the past 2 hours",
                     "logs from 2026-08-11",
                     "how many alerts since Monday"):
        args, dropped = scrub({"date_filter": "x", "state_filter": "open"}, question, [])
        assert not dropped, f"{question!r} names a period but the guard stripped it"
        assert args.get("date_filter") == "x"


def test_guard_accepts_a_period_named_earlier_in_the_conversation():
    """After 'show me logs for yesterday', a bare 'and errors?' is still about yesterday."""
    from agent.arg_guard import scrub
    history = [{"role": "user", "content": "show me logs for yesterday"},
               {"role": "assistant", "content": "There were 2.2 million entries."}]
    args, dropped = scrub({"date_filter": "yesterday", "log_level": "error"},
                          "and errors?", history)
    assert not dropped and args["date_filter"] == "yesterday"


def test_guard_does_not_treat_now_as_a_time_window():
    """'currently' describes live state, not a window — it must not admit a date."""
    from agent.arg_guard import scrub
    for question in ("How many tickets are unassigned currently?",
                     "what is down right now",
                     "which VMs are running now"):
        args, dropped = scrub({"date_filter": "last 10 days"}, question, [])
        assert dropped, f"{question!r} wrongly admitted a date filter"
        assert "date_filter" not in args


def test_guard_leaves_untouched_arguments_alone():
    from agent.arg_guard import scrub
    original = {"state_filter": "open", "priority_filter": "2", "group_by": "agent"}
    args, dropped = scrub(dict(original), "How many priority 2 tickets by agent?", [])
    assert args == original and not dropped


def test_guard_keeps_rate_window_for_metric_queries():
    """range_window sizes a rate average; it does not restrict which rows count."""
    from agent.arg_guard import scrub
    args, _ = scrub({"metric": "cpu", "range_window": "5m"},
                    "what is the cpu utilisation of server01", [])
    assert args["range_window"] == "5m"


def test_guard_handles_empty_and_malformed_input():
    from agent.arg_guard import scrub
    assert scrub({}, "anything", []) == ({}, [])
    assert scrub(None, "anything", []) == (None, [])


def test_nodes_actually_apply_the_guard():
    import inspect
    from agent.nodes import simple_path, complex_path
    for module in (simple_path, complex_path):
        assert "scrub(" in inspect.getsource(module), \
            f"{module.__name__} does not validate the model's arguments"


# --- credential store (offline; uses a throwaway path) ----------------------
import contextlib


@contextlib.contextmanager
def _isolated_store(tmp_name="iqcore_test_creds.json"):
    """A scratch credential file, with the process environment put back after.

    save() deliberately writes into os.environ — that is how a saved config
    reaches the tools. In a test that side effect leaks: fake hostnames like
    zbx.example escaped into the live tool tests running later in the same
    process and broke them. Snapshot and restore.
    """
    import os, tempfile, importlib
    from agent import config_store as store

    saved_env = {key: os.environ.get(key) for key in store.ALL_KEYS}
    saved_path = os.environ.get("IQCORE_CONFIG_PATH")
    path = os.path.join(tempfile.gettempdir(), tmp_name)
    if os.path.exists(path):
        os.remove(path)

    os.environ["IQCORE_CONFIG_PATH"] = path
    importlib.reload(store)
    try:
        yield store, path
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if saved_path is None:
            os.environ.pop("IQCORE_CONFIG_PATH", None)
        else:
            os.environ["IQCORE_CONFIG_PATH"] = saved_path
        importlib.reload(store)
        try:
            os.remove(path)
        except OSError:
            pass


def test_config_store_saves_and_reloads():
    with _isolated_store() as (store, _):
        assert store.is_configured() is False
        store.save({"ZABBIX_URL": "http://zbx.example:8080",
                    "ZABBIX_API_TOKEN": "secret-token"})
        assert store.is_configured() is True
        assert store.load()["ZABBIX_URL"] == "http://zbx.example:8080"


def test_config_store_blank_means_unchanged():
    """The UI shows masked secrets; blank must not wipe the stored value."""
    with _isolated_store("iqcore_test_creds2.json") as (store, _):
        store.save({"ZABBIX_API_TOKEN": "keep-me", "ZABBIX_URL": "http://a"})
        store.save({"ZABBIX_API_TOKEN": "", "ZABBIX_URL": "http://b"})
        config = store.load()
        assert config["ZABBIX_API_TOKEN"] == "keep-me", "blank erased a stored secret"
        assert config["ZABBIX_URL"] == "http://b"


def test_config_store_masks_secrets_but_not_urls():
    with _isolated_store("iqcore_test_creds3.json") as (store, _):
        store.save({"ZABBIX_URL": "http://zbx.example:8080",
                    "ZABBIX_API_TOKEN": "abcdefghijklmnop"})
        masked = store.masked()
        assert masked["ZABBIX_URL"] == "http://zbx.example:8080", "URLs stay readable"
        assert "abcdefghijklmnop" not in masked["ZABBIX_API_TOKEN"]
        assert masked["ZABBIX_API_TOKEN"].endswith("mnop"), "last 4 chars aid recognition"


def test_config_store_applies_to_environment():
    """Tools read os.getenv at call time, so saving must update the process env."""
    import os
    with _isolated_store("iqcore_test_creds4.json") as (store, _):
        store.save({"PROMETHEUS_URL": "http://prom.example:9090"})
        assert os.environ["PROMETHEUS_URL"] == "http://prom.example:9090"


def test_config_store_restores_environment_after_use():
    """The isolation helper itself must not leak fake hosts into later tests."""
    import os
    before = os.environ.get("ZABBIX_URL")
    with _isolated_store("iqcore_test_creds6.json") as (store, _):
        store.save({"ZABBIX_URL": "http://leaked.example:8080"})
    assert os.environ.get("ZABBIX_URL") == before, "test isolation leaked into the process"


def test_config_save_clears_every_cache():
    """Stale data from the previous credentials would look like a silent failure."""
    with _isolated_store("iqcore_test_creds5.json") as (store, _):
        result = store.save({"ZAMMAD_URL": "http://zammad.example"})
        for expected in ("zammad tickets", "prometheus nodes", "live schema",
                         "device index", "answers"):
            assert expected in result["caches_cleared"], f"{expected} cache not cleared"


def test_config_field_groups_cover_every_system():
    from agent import config_store
    systems = {group[0] for group in config_store.FIELD_GROUPS}
    assert systems == {"Zabbix", "Zammad", "Prometheus", "OpenSearch", "Ceph",
                       "Ceph container logs", "Groq"}
    for key in ("ZABBIX_API_TOKEN", "ZAMMAD_PASS", "PROMETHEUS_PASS", "PROMETHEUS_TOKEN",
                "OPENSEARCH_PASS", "CEPH_PASS", "OPENSEARCH2_PASS", "GROQ_API_KEY"):
        assert key in config_store.SECRET_KEYS, f"{key} must be treated as a secret"

    # A system reachable by a tool but absent from the form can only be
    # configured by editing a file on the server, which is the thing the sidebar
    # exists to avoid.
    from agent.tool_registry import TOOL_REGISTRY
    from agent.config_test import TESTS
    assert "query_ceph" in TOOL_REGISTRY and "Ceph" in TESTS, (
        "Ceph is queryable but has no connection test in the sidebar")


def test_config_test_messages_are_actionable():
    """Error text should say what to change, not name a Python exception."""
    from agent.config_test import _describe
    import requests
    assert "http://" in _describe(requests.exceptions.InvalidSchema("x"))
    assert "not with JSON" in _describe(ValueError("JSONDecodeError: Expecting value"))
    assert "could not connect" in _describe(requests.exceptions.ConnectionError("x"))


def test_cache_reports_stats():
    from agent import answer_cache
    answer_cache.clear()
    answer_cache.put("q", {"final_answer": "a", "complexity": "simple"}, [])
    answer_cache.get("q", [])
    answer_cache.get("never asked", [])
    stats = answer_cache.stats()
    assert stats["hits"] >= 1 and stats["misses"] >= 1
    assert 0.0 <= stats["hit_rate"] <= 1.0


@pytest.mark.llm
def test_every_graph_node_announces_itself():
    """The live view is driven by node events, not by parsing status prose.

    Status messages are written for a person to read and change freely; these
    carry the node's identity. A node that stops emitting them would leave the
    drawing stuck mid-run with no error anywhere.
    """
    import threading
    from agent import streaming
    from agent.graph import COMPILED_GRAPH

    emitter = streaming.Emitter()
    seen = []

    def run():
        streaming.set_emitter(emitter)
        try:
            COMPILED_GRAPH.invoke({
                "question": "how many host groups are configured in zabbix?",
                "conversation_history": [], "complexity": None,
                "simple_tool_choice": None, "tool_results_raw": [],
                "dataframes": [], "tools_called": [], "final_answer": None,
            })
            emitter.finish({})
        except Exception as exc:            # a failed run must still close its nodes
            emitter.fail(str(exc))

    threading.Thread(target=run, daemon=True).start()
    while True:
        event = emitter.events.get()
        if event is streaming.DONE:
            break
        if event.get("type") == "node":
            seen.append((event["node"], event["phase"]))

    entered = [n for n, phase in seen if phase == "enter"]
    exited = [n for n, phase in seen if phase == "exit"]
    assert "classify" in entered, f"classify never announced itself: {seen}"
    # Every node that opened must close, or the drawing spins forever.
    assert sorted(entered) == sorted(exited), f"unbalanced node events: {seen}"


def test_node_events_are_silent_when_not_streaming():
    """The context manager must no-op outside a streamed request."""
    from agent import streaming
    assert streaming.get_emitter() is None
    with streaming.node("classify"):
        pass          # must not raise without an emitter
    streaming.tool_started("query_zabbix")


def test_no_tool_accepts_an_inert_question_parameter():
    """Every query tool used to accept `question` and no tool body read it.

    That is worse than dead code. Asked "is there a ticket for the ceph network
    alert", the extractor put 'ceph network alert' into `question`, where it was
    silently discarded — so the call filtered by state alone while looking
    scoped, and the answer married the estate's open-ticket count to a subject
    it had never filtered on.
    """
    from agent.tool_schemas import TOOL_SCHEMAS
    offenders = [t["function"]["name"] for t in TOOL_SCHEMAS
                 if "question" in (t["function"].get("parameters") or {}).get("properties", {})]
    assert not offenders, f"these tools accept an unread `question`: {offenders}"


def test_classifier_carries_the_cross_system_test():
    """A question spanning two systems must not be classified simple.

    "is there a ticket open for the ceph network alert" was routed to
    query_zammad alone, so Ceph was never consulted and the answer was
    confidently wrong.
    """
    from agent.nodes.classify import CLASSIFY_PROMPT
    assert "DECISIVE TEST for complexity" in CLASSIFY_PROMPT
    assert "TWO systems" in CLASSIFY_PROMPT
    assert "ceph network alert" in CLASSIFY_PROMPT, (
        "the worked cross-system example is gone; it is what stops the small "
        "model reading a two-system question as one short question")


def test_telemetry_records_nothing_outside_a_measured_request():
    """The recorder is per-request; calls outside one must not raise or leak."""
    from agent import telemetry
    telemetry.note("classify", "m", None, 0.1)          # no recorder set
    with telemetry.measure("classify", "m"):
        pass
    assert telemetry.summary() in ({}, None) or telemetry.summary().get("calls") is not None


def test_telemetry_aggregates_by_operation_and_totals():
    from agent import telemetry

    class FakeMessage:
        def __init__(self, i, o, r=0):
            self.usage_metadata = {"input_tokens": i, "output_tokens": o,
                                   "total_tokens": i + o,
                                   "output_token_details": {"reasoning": r}}

    recorder = telemetry.start()
    recorder.note("classify", "small", FakeMessage(1000, 50, 30), 0.9)
    recorder.note("extract", "small", FakeMessage(1100, 60, 20), 0.7)
    recorder.note("synthesise", "large", FakeMessage(400, 120, 60), 1.2)

    summary = telemetry.summary()
    assert summary["model_calls"] == 3
    assert summary["total_tokens"] == 1050 + 1160 + 520
    assert summary["reasoning_tokens"] == 110
    # Ordered by cost, so the expensive operation is the first thing read.
    assert summary["by_operation"][0]["operation"] == "extract"
    assert sum(e["share_pct"] for e in summary["by_operation"]) == 100.0
    assert summary["minute_budget_pct"] > 0


def test_telemetry_counts_a_call_that_returned_no_usage():
    """A call with no usage figures must show as a gap, not improve the total."""
    from agent import telemetry
    recorder = telemetry.start()
    recorder.note("orchestrate", "large", None, 0.5)
    summary = telemetry.summary()
    assert summary["model_calls"] == 1
    assert summary["total_tokens"] == 0
    assert summary["unmeasured_calls"] == ["orchestrate"], (
        "an unmeasured call must be named, or the total silently understates")


def test_telemetry_failed_call_is_still_recorded():
    """A call that raised still spent tokens; hiding it flatters the total."""
    from agent import telemetry
    telemetry.start()
    try:
        with telemetry.measure("orchestrate", "large"):
            raise RuntimeError("groq said no")
    except RuntimeError:
        pass
    assert telemetry.summary()["model_calls"] == 1


def test_cached_answers_report_zero_tokens():
    """A cache hit spends nothing; replaying the original cost double-counts."""
    from agent import answer_cache
    answer_cache.clear()
    answer_cache.put("how many pools", {"final_answer": "7",
                                        "telemetry": {"total_tokens": 2808,
                                                      "calls": [{"operation": "classify"}]}})
    hit = answer_cache.get("how many pools")
    assert hit["cached"] is True
    assert hit["telemetry"]["total_tokens"] == 0
    assert hit["telemetry"]["served_from_cache"] is True
    assert hit["telemetry"]["original_total_tokens"] == 2808


def test_telemetry_never_breaks_the_answer():
    """Measurement is subordinate to answering.

    stream_text merges chunks only to recover usage figures. A chunk type that
    cannot be added must leave the call unmeasured, not lose the answer.
    """
    from agent import streaming, telemetry

    class Unmergeable:
        content = "hello "

    class Unmergeable2:
        content = "world"

    class FakeClient:
        def stream(self, messages):
            yield Unmergeable()
            yield Unmergeable2()

    emitter = streaming.Emitter()
    streaming.set_emitter(emitter)
    telemetry.start()
    try:
        text = streaming.stream_text(FakeClient(), [], operation="synthesise", model="m")
    finally:
        streaming.set_emitter(None)

    assert text == "hello world", "the answer was lost to a telemetry concern"
    summary = telemetry.summary()
    assert summary["model_calls"] == 1
    assert summary["unmeasured_calls"] == ["synthesise"]


def test_storage_vocabulary_names_its_system():
    """The classifier over-asked on questions only one system could own.

    "Give me the snapshot details" is an official Ceph question and came back as
    "which system did you mean?", because no system is named in the sentence.
    Only Ceph has snapshots, OSDs and pools, so that is not ambiguity — this is
    the ceiling over the model's judgement, mirroring looks_ambiguous below it.
    """
    from agent import clarify
    for question in ("show me the osds", "how full are the pools",
                     "is the rbd image there", "what does the crush rule say"):
        assert clarify.resolves_to_one_tool(question) == "query_ceph", question
    assert clarify.resolves_to_one_tool("how many tickets are open") == "query_zammad"

    # "snapshots" was resolved here until the routing score showed what that
    # costs: a resolver blind to shared nouns sent "Get me Prometheus URL?" to
    # Prometheus, when Ceph's dashboard is what records that URL. Shared nouns
    # now go to the model, which reads the recorded default and can weigh the
    # whole sentence rather than matching one word.
    assert clarify.resolves_to_one_tool("Give me the snapshot details") == ""
    assert clarify.resolves_to_one_tool("Get me Prometheus URL?") == ""


def test_two_systems_named_still_falls_through_to_the_question():
    """Resolving must never break the tie when the tie is real."""
    from agent import clarify
    for question in ("how many snapshots and how many tickets?",
                     "is there a ticket for the ceph alert"):
        assert clarify.resolves_to_one_tool(question) == "", question
    # Nothing routable at all stays unrouted.
    assert clarify.resolves_to_one_tool("is it down?") == ""


def test_vocabulary_matches_whole_words_only():
    """A routing rule that fires on a substring is worse than no rule.

    The word boundaries were lost once already — \b is a valid string escape and
    became a backspace character, leaving a pattern that silently matched nothing.
    """
    from agent import clarify
    assert clarify.resolves_to_one_tool("liverpool are playing") == ""
    assert clarify.resolves_to_one_tool("the whirlpool broke") == ""
    assert clarify.resolves_to_one_tool("how many pools are there") == "query_ceph"


def test_router_can_see_every_system_its_tools_answer_for():
    """Routing summaries are matching hooks for the model, not documentation.

    Compressing them for tokens dropped half of Ceph's surface: "snapshot" did
    not appear anywhere the classifier could see it, so snapshot questions were
    routed to Zabbix or fell through to the complex path.
    """
    from agent.tool_registry import ROUTING_SUMMARIES
    ceph = ROUTING_SUMMARIES["query_ceph"].lower()
    for word in ("snapshot", "cephfs", "crush", "osd", "pool", "host", "log"):
        assert word in ceph, f"router cannot see Ceph {word}"
    zammad = ROUTING_SUMMARIES["query_zammad"].lower()
    for word in ("search", "queue", "conversation"):
        assert word in zammad, f"router cannot see Zammad {word}"


def test_routing_corpus_covers_every_system():
    """A routing score that ignores a system cannot catch it being misrouted.

    Zammad, Prometheus and OpenSearch had no natural-language questions at all
    before this — only internal labels like "opened last 10 days", which nobody
    would type — so a corpus built from golden_queries alone would have scored
    two systems out of five.
    """
    import routing_cases
    from agent.tool_registry import TOOL_REGISTRY

    covered = {tool for _, tool, _ in routing_cases.ROUTING_CASES if tool}
    for tool in ("query_ceph", "query_zabbix", "query_zammad",
                 "query_prometheus", "query_opensearch"):
        assert tool in covered, f"no routing cases for {tool}"

    for question, tool, complexity in routing_cases.ROUTING_CASES:
        assert tool is None or tool in TOOL_REGISTRY, f"{question}: unknown {tool}"
        assert complexity in ("simple", "complex"), f"{question}: {complexity}"


def test_routing_corpus_has_no_duplicate_questions():
    """The same question scored twice weights it twice and hides the rest."""
    import routing_cases
    questions = [q for q, _, _ in routing_cases.ROUTING_CASES]
    duplicates = {q for q in questions if questions.count(q) > 1}
    assert not duplicates, f"duplicated: {duplicates}"


def test_collision_cases_prefer_something_they_accept():
    """A preferred answer outside the accepted set is a typo, not a decision."""
    import routing_cases
    for question, accepted, preferred in routing_cases.COLLISION_CASES:
        assert len(accepted) > 1, f"{question}: not a collision"
        assert preferred in accepted, f"{question}: prefers {preferred}, not in {accepted}"


def test_vague_questions_are_caught_without_a_model():
    """The deterministic floor should hold these on its own.

    If looks_ambiguous already catches them, a classifier that answers them
    anyway is overriding code that was right — worth knowing separately from
    the model simply being wrong.
    """
    from agent import clarify
    import routing_cases
    for question in routing_cases.SHOULD_ASK:
        assert clarify.looks_ambiguous(question), f"floor missed: {question!r}"


def test_every_capability_is_something_the_router_can_see():
    """A tool branch nobody can say is a branch nobody can reach.

    The classifier reads one-line summaries, not docstrings. Ceph's summary
    listed capacity, pools, OSDs, images and health but not snapshots, so
    "give me the snapshot details" had nothing to attach to and went to Zabbix.
    This walks each tool's surface and fails when a capability has no words in
    its summary — the exact shape of that bug, caught before a user finds it.
    """
    from agent.vocabulary import SURFACE
    from agent.tool_registry import ROUTING_SUMMARIES

    missing = []
    for tool, capabilities in SURFACE.items():
        summary = ROUTING_SUMMARIES[tool].lower()
        for capability, words in capabilities.items():
            if not any(word.lower() in summary for word in words):
                missing.append(f"{tool}.{capability} (wanted one of {words})")
    assert not missing, "router cannot see: " + "; ".join(missing)


def test_declared_query_types_all_have_vocabulary():
    """Adding a branch must fail the suite until someone says what it is called.

    Reads the query_type values each tool advertises in its own docstring —
    which is what argument extraction reads too, so anything absent there is
    unreachable regardless — and requires an entry for each.
    """
    import re
    from agent.vocabulary import SURFACE, PLUMBING
    from agent.tool_registry import TOOL_REGISTRY

    for tool in ("query_ceph", "query_zabbix"):
        doc = TOOL_REGISTRY[tool]["description"] or ""
        declared, capturing = set(), False
        for line in doc.splitlines():
            if re.search(r"\bquery_type\b", line):
                capturing = True
            elif capturing and line.strip() and not line.startswith((" ", "\t")):
                capturing = False
            if capturing:
                declared.update(re.findall(r"'([a-z_]{3,})'", line))
        known = set(SURFACE[tool]) | PLUMBING
        assert declared <= known, f"{tool}: no vocabulary for {sorted(declared - known)}"


def test_collisions_are_decided_not_left_to_chance():
    """Every shared noun needs a recorded default and a reason.

    Both corpora claim "hosts" with nothing in either sentence to tell them
    apart. Whichever way that resolves, it should be a decision somebody made.
    """
    from agent.vocabulary import COLLISIONS, SURFACE, collision_guidance
    for entry in COLLISIONS:
        assert len(entry["systems"]) > 1, entry["noun"]
        assert entry["prefer"] in entry["systems"], entry["noun"]
        assert entry["note"].strip(), f"{entry['noun']}: no reason recorded"
        for tool in entry["systems"]:
            assert tool in SURFACE, f"{entry['noun']}: unknown {tool}"
    # The compact form goes in the prompt; the long form carries the reasoning
    # for whoever reads the file. Both must name the noun and its default.
    compact = collision_guidance()
    assert '"hosts" -> zabbix' in compact, compact[:200]
    assert len(compact) < len(collision_guidance(compact=False))
    assert "Ceph has three cluster hosts" in collision_guidance(compact=False)


def test_anchor_words_cover_the_storage_vocabulary():
    """The ambiguity floor did not know the word "ceph" once.

    "is ceph healthy?" was answered with a clarifying question that did not
    even list Ceph among the options. Deriving the words from the surface means
    a new capability widens the floor automatically.
    """
    from agent.vocabulary import anchor_words
    words = set(anchor_words())
    for word in ("snapshots", "pools", "osds", "cephfs", "crush", "tickets",
                 "containers", "logs", "hosts"):
        assert word in words, f"anchor words missing {word!r}"


def test_route_schema_only_allows_real_tool_names():
    """An invented tool name cost the user an answer, silently.

    Nothing checked the string the classifier returned against the registry.
    simple_path looked it up, found nothing, and asked which system was meant —
    indistinguishable from a genuinely vague question. The schema enumerates
    the names, so the wrong one cannot be expressed.
    """
    from agent.nodes.classify import _route_schema
    from agent.tool_registry import TOOL_REGISTRY
    properties = _route_schema()["function"]["parameters"]["properties"]
    assert set(properties["tool"]["enum"]) == set(TOOL_REGISTRY) | {"none"}
    assert properties["complexity"]["enum"] == ["simple", "complex"]
    assert "none" in properties["ambiguous"]["enum"]


class _Reply:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


def test_routing_decision_is_read_from_the_forced_call():
    from agent.nodes.classify import _read_decision
    reply = _Reply(tool_calls=[{"name": "route", "args": {
        "complexity": "simple", "tool": "query_ceph", "ambiguous": "none"}}])
    assert _read_decision(reply) == ("simple", "query_ceph", "")


def test_routing_decision_falls_back_to_loose_json():
    """Kept so a provider refusing tool_choice degrades instead of failing."""
    from agent.nodes.classify import _read_decision
    reply = _Reply(content='{"complexity":"complex","tool":null,"ambiguous":"system"}')
    assert _read_decision(reply) == ("complex", None, "system")


def test_an_unknown_tool_name_never_reaches_the_router():
    from agent.nodes.classify import _read_decision
    reply = _Reply(content='{"complexity":"simple","tool":"query_splunk","ambiguous":"none"}')
    complexity, tool, axis = _read_decision(reply)
    assert tool is None, "an unregistered tool name must not be routed to"


def test_unreadable_routing_output_is_reported_not_swallowed():
    """It became "complex" with no tool and no error.

    A prompt change that broke parsing would have read as every question
    suddenly costing several times more, with nothing to explain it.
    """
    from agent.nodes.classify import _read_decision
    assert _read_decision(_Reply(content="I think this is a Zabbix question")) is None
    assert _read_decision(_Reply(content="")) is None
    assert _read_decision(_Reply(content='["not", "an", "object"]')) is None


def test_blocking_endpoints_are_not_declared_async():
    """`async def` on a handler that blocks stalls every other request.

    /config and /config/test open connections to five systems and Groq, each
    with its own timeout. Declared `async def`, they ran on the event loop, so
    while one was working nothing else was served — including /health, the one
    call you would make to ask whether the server was alive.

    A plain `def` handler is run in a worker thread by FastAPI, which is what
    /chat/stream already arranges by hand.

    This asserts the invariant rather than the behaviour. A behavioural test was
    written first and thrown away: TestClient drives each request through its
    own portal, so an async handler that blocks looks identical to one that does
    not, and the test passed against the bug it was meant to catch. Proving it
    properly needs a real uvicorn process, which is not worth the flakiness.
    """
    import inspect
    import main
    for name in ("test_config", "save_config", "dashboard_source"):
        handler = getattr(main, name)
        assert not inspect.iscoroutinefunction(handler), (
            f"{name} is async but blocks; FastAPI cannot move it off the event loop")

    # The streaming endpoint is the exception and must stay async: it returns a
    # StreamingResponse and hands the graph to a thread itself.
    assert inspect.iscoroutinefunction(main.chat_stream)


# --- flaky connections ------------------------------------------------------
class _Flaky:
    """Stands in for requests.request: fails a set number of times, then works."""

    def __init__(self, error, failures=1):
        self.error, self.failures, self.calls = error, failures, 0

    def __call__(self, method, url, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return f"response for {method} {url}"


def _with_fake(monkeyed, fn):
    """Run fn with clients.requests.request swapped for `monkeyed`."""
    from tools import clients
    original = clients.requests.request
    clients.requests.request = monkeyed
    try:
        return fn()
    finally:
        clients.requests.request = original


def test_a_timed_out_request_is_tried_again():
    """Ceph answered the same call in 45s (timed out), then 25s, then 10.5s.

    One attempt turned that into "could not connect to Ceph" after a 45-second
    wait, and the question had already spent a full round of model calls, so the
    user paid twice — once for the failed answer, once for retyping it.
    """
    import requests
    from tools import clients
    fake = _Flaky(requests.exceptions.ReadTimeout("read timed out"))
    result = _with_fake(fake, lambda: clients.request("GET", "http://x/y", system="Ceph"))
    assert fake.calls == 2, "a timeout should get a second attempt"
    assert result == "response for GET http://x/y"


def test_a_refused_connection_is_tried_again():
    import requests
    from tools import clients
    fake = _Flaky(requests.exceptions.ConnectionError("refused"))
    _with_fake(fake, lambda: clients.request("GET", "http://x/y"))
    assert fake.calls == 2


def test_http_errors_are_never_retried():
    """What is NOT retried matters as much as what is.

    Every client calls raise_for_status, so 401, 404, 429 and 500 arrive as
    HTTPError, never as Timeout. 429 especially: the models are capped per
    minute AND per day, so retrying a rate limit spends the remainder faster
    while never succeeding.
    """
    import requests
    from tools import clients
    for error in (requests.exceptions.HTTPError("429 Too Many Requests"),
                  requests.exceptions.HTTPError("401 Unauthorized"),
                  requests.exceptions.HTTPError("500 Server Error")):
        fake = _Flaky(error, failures=99)
        try:
            _with_fake(fake, lambda: clients.request("GET", "http://x/y"))
        except requests.exceptions.HTTPError:
            pass
        assert fake.calls == 1, f"{error} must not be retried, got {fake.calls} attempts"


def test_a_real_outage_still_fails():
    """Retry must not turn a dead system into a hang or a false success."""
    import requests
    from tools import clients
    fake = _Flaky(requests.exceptions.ReadTimeout("down"), failures=99)
    try:
        _with_fake(fake, lambda: clients.request("GET", "http://x/y"))
        assert False, "an unreachable system must still raise"
    except requests.exceptions.ReadTimeout:
        pass
    assert fake.calls == 2, "one extra attempt, not three"


def test_retries_can_be_switched_off():
    """The evals turn this off so a failure stays a failure."""
    import os
    import requests
    from tools import clients
    original = os.environ.get("IQCORE_HTTP_RETRIES")
    os.environ["IQCORE_HTTP_RETRIES"] = "0"
    try:
        fake = _Flaky(requests.exceptions.ReadTimeout("x"), failures=99)
        try:
            _with_fake(fake, lambda: clients.request("GET", "http://x/y"))
        except requests.exceptions.ReadTimeout:
            pass
        assert fake.calls == 1
    finally:
        if original is None:
            os.environ.pop("IQCORE_HTTP_RETRIES", None)
        else:
            os.environ["IQCORE_HTTP_RETRIES"] = original


def test_every_client_goes_through_the_retry_helper():
    """A client calling requests directly is one that silently does not retry."""
    import pathlib
    for path in ("backend/tools/clients.py", "backend/tools/zabbix/client.py"):
        source = pathlib.Path(path).read_text(encoding="utf-8")
        body = source.split("def request(", 1)[-1] if "def request(" in source else source
        assert "requests.get(" not in body and "requests.post(" not in body, (
            f"{path} calls requests directly, bypassing the retry helper")


# --- ceph <-> prometheus VM correlation --------------------------------------
def test_ceph_vm_images_only_reads_the_vms_pool():
    """A UUID can appear in a Glance image name too — that is how Glance names
    uploads — so scoping to the vms pool is what keeps this a VM-storage join
    rather than a coincidental string match against an unrelated pool."""
    from tools.vm_storage_correlation import _ceph_vm_images
    uuid = "1a05a978-3dd7-488a-8801-d234565a8ed6"
    images = [
        {"pool_name": "vms", "value": [
            {"name": f"{uuid}_disk", "size": 8_600_000_000, "disk_usage": 10_000_000}]},
        {"pool_name": "images", "value": [
            {"name": f"{uuid}", "size": 999, "disk_usage": 1}]},
        {"pool_name": "volumes", "value": [
            {"name": "volume-2aca0eeb-c4d6-454c-a611-ba883cd06873", "size": 1, "disk_usage": 1}]},
    ]
    matched = _ceph_vm_images(images)
    assert list(matched.keys()) == [uuid], "images and volumes pools must not match"
    assert matched[uuid]["pool"] == "vms"
    assert matched[uuid]["provisioned_GB"] == 8.6


def test_vm_correlation_joins_on_the_shared_uuid():
    """The join itself: Ceph's derived UUID against Prometheus's instance_id.

    Uses fake clients rather than the real ones, so this runs offline. The
    live match (jammy, 1a05a978-...) is checked separately in
    test_tools_live.py against the real environment.
    """
    import sys
    import pandas as pd
    from tools import vm_storage_correlation as vsc

    matched_uuid = "1a05a978-3dd7-488a-8801-d234565a8ed6"
    unmatched_uuid = "45a3786d-3dc6-4c4f-afa6-282ad4000fb8"

    class _FakeProm:
        def instant(self, query):
            if "virtual_cpus" in query:
                return [{"metric": {"domain": "instance-00000015"}, "value": [0, "2"]}]
            if "cpu_time_seconds_total" in query:
                return [{"value": [0, "0.05"]}]
            if "memory_stats_used_percent" in query:
                return [{"value": [0, "32.0"]}]
            return []

    class _FakeCeph:
        def images(self):
            return [{"pool_name": "vms", "value": [
                {"name": f"{matched_uuid}_disk", "size": 8_600_000_000, "disk_usage": 10_000_000}]}]

    def fake_inventory(client):
        return [
            {"vm": "jammy", "domain": "instance-00000015", "instance_id": matched_uuid,
             "running": True, "hypervisor": "server01"},
            {"vm": "windows", "domain": "instance-00000082", "instance_id": unmatched_uuid,
             "running": True, "hypervisor": "server01"},
        ]

    import types
    fake_prom_tool = types.ModuleType("tools.prometheus_tool")
    fake_prom_tool._vm_inventory = fake_inventory
    original = sys.modules.get("tools.prometheus_tool")
    sys.modules["tools.prometheus_tool"] = fake_prom_tool

    original_ceph, original_prom = vsc.CephClient, vsc.PrometheusClient
    vsc.CephClient = lambda: _FakeCeph()
    vsc.PrometheusClient = lambda: _FakeProm()
    try:
        df = vsc.correlate_vm_storage(vm_filter=None)
    finally:
        vsc.CephClient, vsc.PrometheusClient = original_ceph, original_prom
        if original is not None:
            sys.modules["tools.prometheus_tool"] = original
        else:
            del sys.modules["tools.prometheus_tool"]

    assert len(df) == 2
    jammy = df[df["vm"] == "jammy"].iloc[0]
    windows = df[df["vm"] == "windows"].iloc[0]
    # bool(), not `is True`: .iloc[0] on a row that mixes bools with NaN-bearing
    # float columns upcasts the whole row, so on_ceph arrives as numpy.bool_,
    # which `is True` silently fails against even though the value is correct.
    assert bool(jammy["on_ceph"]), "jammy's UUID matches a Ceph image and must join"
    assert jammy["ceph_provisioned_GB"] == 8.6
    assert jammy["cpu_percent"] > 0, "a matched VM must carry compute figures"
    assert not bool(windows["on_ceph"]), "windows has no Ceph image and must not join"
    # pandas stores a missing float as NaN, not None, once the column also
    # holds real numbers — pd.isna() is the correct check either way.
    assert pd.isna(windows["ceph_provisioned_GB"]), "an unmatched VM keeps a row, not a false one"


def test_a_real_tool_outranks_its_own_contradicted_axis():
    """Found live: "what is the current unresolved case count" got tool=query_zammad
    and ambiguous="system" in the SAME model response — the schema does not force
    these two fields to agree. The old code only trusted a model-given tool when
    the model gave no axis at all, so the correct tool was discarded and the user
    got a clarifying question for something already answered correctly.

    Regression-tests the fix at the point that actually matters: the model names a
    real tool, the raw question text matches no known word (so the deterministic
    floor also independently wants to ask), and the tool must still win. Getting
    the ordering wrong here was the trap — clearing axis before the floor runs
    just lets the floor set it right back, since the floor's job is exactly to
    fire on text it does not recognise, which is what a genuine paraphrase is.
    """
    import agent.nodes.classify as classify_mod

    class _StubReply:
        tool_calls = [{"name": "route", "args": {
            "complexity": "simple", "tool": "query_zammad", "ambiguous": "system"}}]
        content = ""

    class _StubBound:
        def invoke(self, messages):
            return _StubReply()

    class _StubClient:
        def __init__(self, model, temperature):
            pass

        def bind_tools(self, tools, tool_choice):
            return _StubBound()

    original = classify_mod.ChatGroq
    classify_mod.ChatGroq = _StubClient
    try:
        # No word here matches vocabulary.anchor_words(), so looks_ambiguous()
        # independently wants to ask too — the exact condition that made the
        # naive fix placement fail.
        from agent import clarify
        question = "what is the current unresolved case count"
        assert clarify.looks_ambiguous(question), "test needs a text the floor also flags"

        state = classify_mod._classify_node_impl({
            "question": question, "conversation_history": []})
    finally:
        classify_mod.ChatGroq = original

    assert state["simple_tool_choice"] == "query_zammad"
    assert state["clarify_axis"] == "", (
        "a real tool from the model's own call must outrank its own axis")


# --- ceph daemon container logs ----------------------------------------------
def test_ceph_daemon_tool_never_offers_priority_as_severity():
    """PRIORITY cannot mean severity in this index and must not be a filter.

    Measured: 60,513 of 61,219 documents are priority 3, because anything a
    container writes to stderr gets 3 regardless of what it says. A severity
    filter built on it returns 99% of the index — the same shape as the "down
    was a dead category" bug, where a plausible-looking field carried no signal.
    Severity is read from the message text instead.
    """
    import inspect
    from tools import ceph_daemon_tool
    signature = inspect.signature(ceph_daemon_tool.check_ceph_daemon_health.func)
    assert "priority" not in signature.parameters
    assert "log_level" not in signature.parameters
    assert "severity" not in signature.parameters


def test_ceph_daemon_tool_aggregates_on_keyword_subfields():
    """Aggregating the bare field returns EMPTY BUCKETS, not an error.

    Every field in this index is `text` with a `.keyword` subfield. A breakdown
    built on the bare name silently comes back blank, which is how a wrong
    answer gets reported confidently. Confirmed by hitting it during probing.
    """
    from tools import ceph_daemon_tool
    assert ceph_daemon_tool.CONTAINER.endswith(".keyword")
    assert ceph_daemon_tool.UNIT.endswith(".keyword")


def test_ceph_daemon_tool_states_its_boundary_against_query_ceph():
    """The whole risk of this tool is being asked a disk question.

    The container index holds no cluster events at all — probing found zero
    occurrences of HEALTH_OK, slow ops, pg, mon and mgr — so a disk question
    routed here would answer nothing, and that zero would be meaningless
    rather than informative. The docstring is what the router reads.
    """
    from tools.ceph_daemon_tool import check_ceph_daemon_health
    doc = check_ceph_daemon_health.description.lower()
    assert "query_ceph" in doc, "the docstring must name where disk questions go"
    for word in ("osd", "disk", "cluster health"):
        assert word in doc, f"the docstring must exclude {word!r} explicitly"


def test_ceph_daemon_client_forces_a_real_total():
    """OpenSearch stops counting at 10,000 and reports exactly that.

    The index holds 61,000+ documents, so without track_total_hits every count
    answers "10000" — a wrong number with no error attached. Three other
    systems in this app already have a silent-truncation trap; this is a fourth.
    """
    import inspect
    from tools.clients import CephDaemonLogClient
    assert "track_total_hits" in inspect.getsource(CephDaemonLogClient.search)


# --- accounts, credits and per-user settings (SQLite) ----------------------
import contextlib as _contextlib


@_contextlib.contextmanager
def _fresh_db(auth_file=None):
    """A brand-new database in a temp dir; the real one is never touched."""
    import os, tempfile
    from agent import config_store, db
    folder = tempfile.mkdtemp()
    names = ("IQCORE_DB_PATH", "IQCORE_SECRET_KEY_PATH", "IQCORE_AUTH_PATH")
    saved = {k: os.environ.get(k) for k in names}
    os.environ["IQCORE_DB_PATH"] = os.path.join(folder, "iqcore.db")
    os.environ["IQCORE_SECRET_KEY_PATH"] = os.path.join(folder, "secret.key")
    os.environ["IQCORE_AUTH_PATH"] = auth_file or os.path.join(folder, "none.json")
    try:
        db.init()
        yield db, folder
    finally:
        config_store.use_user(None)      # never leak a signed-in user into later tests
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_db_schema_is_idempotent_and_enforces_foreign_keys():
    import sqlite3
    with _fresh_db() as (db, _):
        db._ready.clear()
        db.init()                                    # a second start: no error
        try:
            db.record_usage(999, "s", "q", 10)       # there is no user 999
            raise AssertionError("foreign keys are not enforced")
        except sqlite3.IntegrityError:
            pass


def test_signup_login_and_sessions():
    from agent import auth_store
    with _fresh_db() as (db, _):
        assert not auth_store.signup("nikunj", "secret123", "not-an-email")["ok"]
        first = auth_store.signup("nikunj", "secret123", "n@example.com")
        second = auth_store.signup("priya", "secret456", "p@example.com")
        assert first["ok"] and second["ok"]
        assert not auth_store.signup("NIKUNJ", "whatever1", "x@example.com")["ok"], "username case"
        assert not auth_store.signup("other", "whatever1", "N@EXAMPLE.com")["ok"], "email case"

        assert auth_store.login("nikunj", "secret123")["role"] == "admin", "first account is admin"
        assert auth_store.login("priya", "secret456")["role"] == "user"
        assert not auth_store.login("priya", "secret123")["ok"], "one user's password on another"
        assert not auth_store.login("ghost", "secret123")["ok"]

        token = db.create_session(first["user_id"])
        assert db.user_for_session(token)["username"] == "nikunj"
        assert db.user_for_session("forged") is None
        db.end_session(token)
        assert db.user_for_session(token) is None, "sign-out must end the session"

        with db.tx() as c:                           # an expired session is no session
            c.execute("INSERT INTO sessions VALUES (?, ?, ?)",
                      (db._digest("old"), first["user_id"], "2000-01-01T00:00:00+00:00"))
        assert db.user_for_session("old") is None


def test_credit_balance_is_computed_not_stored():
    from agent import auth_store
    with _fresh_db() as (db, _):
        uid = auth_store.signup("nikunj", "secret123", "n@example.com")["user_id"]
        assert db.balance(uid)["allocated_paise"] == 1_000_000, "new accounts get Rs 10,000"
        for session, tokens in (("s1", 5000), ("s1", 1000), ("s2", 0)):
            db.record_usage(uid, session, "q", tokens)
        b = db.balance(uid)
        assert b["answers"] == 3, "a cached answer still counts as an answer"
        assert b["spent_paise"] == 6000 * db.PAISE_PER_TOKEN
        assert b["remaining_paise"] == 1_000_000 - b["spent_paise"]
        assert db.set_allocation(uid, 500)
        assert db.balance(uid)["remaining_paise"] < 0, "show-only: a balance may go negative"
        with db.tx() as c:
            per = dict(c.execute("SELECT session_id, SUM(paise) FROM usage "
                                 "WHERE user_id = ? GROUP BY session_id", (uid,)).fetchall())
        assert per == {"s1": 6000 * db.PAISE_PER_TOKEN, "s2": 0}


def test_settings_are_per_user_and_secrets_are_sealed():
    """The leak this whole change exists to prevent."""
    from agent import answer_cache, auth_store, config_store
    from tools.clients import Cached
    with _fresh_db() as (db, _):
        a = auth_store.signup("alice", "secret123", "a@example.com")["user_id"]
        b = auth_store.signup("bob", "secret456", "b@example.com")["user_id"]
        config_store.user_save(a, {"ZABBIX_URL": "http://zbx-a",
                                   "ZABBIX_API_TOKEN": "token-alice-1234", "CEPH_URL": ""})
        config_store.user_save(b, {"ZAMMAD_URL": "http://zammad-b"})

        with db.tx() as c:
            raw = " ".join(r[0] for r in c.execute("SELECT settings FROM service_credentials"))
        assert "token-alice-1234" not in raw and "enc:" in raw, "secrets must be encrypted at rest"
        assert set(db.load_settings(a)) == {"Zabbix"}, "an empty section is not a connection"

        probe = Cached("isolation probe", 60)
        try:
            config_store.use_user(a)
            assert config_store.get("ZABBIX_API_TOKEN") == "token-alice-1234"
            config_store.require("Zabbix")
            probe.put("alice's devices")
            answer_cache.put("how many hosts", {"final_answer": "alice",
                                                "complexity": "simple"}, [])

            config_store.use_user(b)
            assert config_store.get("ZABBIX_API_TOKEN") == "", "bob must not see alice's token"
            try:
                config_store.require("Zabbix")
                raise AssertionError("bob has no Zabbix and must be told so")
            except config_store.NotConnected:
                pass
            assert probe.get() is None, "bob must not be served alice's cached data"
            assert answer_cache.get("how many hosts", []) is None, "nor alice's answer"

            config_store.use_user(a)
            assert probe.get() == "alice's devices"
        finally:
            Cached._registry.remove(probe)


def test_legacy_json_files_are_imported_once():
    """Existing users keep their passwords; the old global credentials become the admin's."""
    import json, os, tempfile
    from agent import auth_store
    salt = "ab" * 16
    auth_file = os.path.join(tempfile.mkdtemp(), "authentication.json")
    with open(auth_file, "w", encoding="utf-8") as f:
        json.dump({"users": [
            {"username": "nikunj", "salt": salt, "hash": auth_store._hash("old-pass1", salt)},
            {"username": "priya", "salt": salt, "hash": auth_store._hash("old-pass2", salt)},
        ]}, f)

    with _isolated_store("iqcore_test_legacy.json") as (store, _):
        store.save({"ZABBIX_URL": "http://zbx.legacy", "ZABBIX_API_TOKEN": "legacy-token",
                    "GROQ_API_KEY": "gsk-system"})
        with _fresh_db(auth_file) as (db, _):
            assert auth_store.login("nikunj", "old-pass1")["role"] == "admin"
            assert auth_store.login("priya", "old-pass2")["role"] == "user"
            settings = db.load_settings(db.find_user("nikunj")["id"])
            assert settings["Zabbix"]["ZABBIX_API_TOKEN"] == "legacy-token"
            assert "Groq" not in settings, "Groq stays a system setting"
            assert db.load_settings(db.find_user("priya")["id"]) == {}
            db._ready.clear()
            db.init()
            assert db.count_users() == 2, "the import runs once, into an empty database"


def test_endpoints_require_a_session_and_admin_is_enforced():
    from fastapi.testclient import TestClient
    import main
    with _fresh_db() as (db, _):
        admin, user = TestClient(main.app), TestClient(main.app)
        assert admin.get("/me").status_code == 401
        assert admin.post("/chat/stream", json={"question": "hi"}).status_code == 401
        assert admin.get("/config").status_code == 401

        assert admin.post("/auth/signup", json={"username": "nikunj", "password": "secret123",
                                                "email": "n@example.com"}).json()["ok"]
        assert user.post("/auth/signup", json={"username": "priya", "password": "secret456",
                                               "email": "p@example.com"}).json()["ok"]
        assert admin.get("/me").json()["role"] == "admin"
        assert user.get("/me").json()["role"] == "user"

        assert user.get("/admin/credits").status_code == 403
        rows = admin.get("/admin/credits").json()["users"]
        priya = next(r for r in rows if r["username"] == "priya")
        assert admin.post("/admin/credits", json={"user_id": priya["user_id"],
                                                  "allocated_rupees": 2500}).json()["ok"]
        assert user.get("/me").json()["allocated_paise"] == 250_000

        assert "Groq" not in {g["system"] for g in user.get("/config").json()["groups"]}
        assert "Groq" in {g["system"] for g in admin.get("/config").json()["groups"]}

        user.post("/auth/logout")
        assert user.get("/me").status_code == 401


# --- saved conversations, pins, history charts ------------------------------
def test_turns_are_saved_trimmed_and_scoped_per_user():
    from agent import auth_store
    with _fresh_db() as (db, _):
        a = auth_store.signup("alice", "secret123", "a@example.com")["user_id"]
        b = auth_store.signup("bob", "secret456", "b@example.com")["user_id"]
        cid = db.new_conversation_id()
        big = {"final_answer": "x", "dataframes": [[{"n": i} for i in range(900)]],
               "tool_results_raw": [{"huge": "y" * 1000}]}
        db.save_turn(a, cid, "first question", "first answer", big)
        db.save_turn(a, cid, "second question", "second answer", {"final_answer": "y"})

        listed = db.list_conversations(a)
        assert listed[0]["id"] == cid and listed[0]["title"] == "first question"
        assert listed[0]["turns"] == 2
        turns = db.load_conversation(a, cid)
        assert [t["question"] for t in turns] == ["first question", "second question"]
        assert len(turns[0]["payload"]["dataframes"][0]) == db.STORED_ROWS, "tables are capped"
        assert "tool_results_raw" not in turns[0]["payload"], "raw tool output is not kept"

        assert db.list_conversations(b) == [], "bob sees none of alice's conversations"
        assert db.load_conversation(b, cid) == [], "nor can he open one by its id"
        assert not db.delete_conversation(b, cid), "nor delete it"
        assert db.delete_conversation(a, cid) and db.load_conversation(a, cid) == []


def test_favourites_are_idempotent_and_per_user():
    from agent import auth_store
    with _fresh_db() as (db, _):
        a = auth_store.signup("alice", "secret123", "a@example.com")["user_id"]
        b = auth_store.signup("bob", "secret456", "b@example.com")["user_id"]
        first = db.add_favourite(a, "is ceph healthy")
        again = db.add_favourite(a, "is ceph healthy")
        assert first["id"] == again["id"] and len(db.list_favourites(a)) == 1
        assert db.list_favourites(b) == []
        assert not db.delete_favourite(b, first["id"]), "bob cannot remove alice's pin"
        assert db.delete_favourite(a, first["id"]) and db.list_favourites(a) == []


def test_conversation_endpoints_hide_other_users():
    from fastapi.testclient import TestClient
    import main
    with _fresh_db() as (db, _):
        alice, bob = TestClient(main.app), TestClient(main.app)
        assert alice.get("/conversations").status_code == 401
        alice.post("/auth/signup", json={"username": "alice", "password": "secret123",
                                         "email": "a@example.com"})
        bob.post("/auth/signup", json={"username": "bob", "password": "secret456",
                                       "email": "b@example.com"})
        cid = db.new_conversation_id()
        db.save_turn(db.find_user("alice")["id"], cid, "q", "a", {"final_answer": "a"})
        assert alice.get(f"/conversations/{cid}").status_code == 200
        assert bob.get(f"/conversations/{cid}").status_code == 404
        assert bob.delete(f"/conversations/{cid}").status_code == 404
        pin = alice.post("/favourites", json={"question": "is ceph healthy"}).json()
        assert bob.delete(f"/favourites/{pin['id']}").status_code == 404
        assert alice.post("/favourites", json={"question": "  "}).status_code == 400


def test_history_rows_are_long_format():
    """One row per series per point, so any number of lines can be drawn."""
    from tools.prometheus_tool import _series_rows
    matrix = [
        {"metric": {"instance": "192.168.1.30:9100"},
         "values": [[1700000000, "10.5"], [1700000060, "12"]]},
        {"metric": {"instance": "192.168.1.31:9100"},
         "values": [[1700000000, "bad"], [1700000060, "3"]]},
    ]
    rows = _series_rows(matrix, "%", {"server02": "192.168.1.30:9100"})
    assert [r["series"] for r in rows] == ["server02", "server02", "192.168.1.31:9100"]
    assert rows[0] == {"time": "2023-11-14T22:13:20Z", "series": "server02",
                       "host": "server02", "value": 10.5, "unit": "%"}


def test_history_is_stripped_unless_a_period_is_asked_for():
    """The extractor fills blanks; a chart nobody asked for is the same bug as a
    date filter nobody asked for."""
    from agent.arg_guard import scrub
    args = {"metric": "cpu", "history_minutes": 60}
    cleaned, dropped = scrub(dict(args), "what's the CPU on server02")
    assert "history_minutes" not in cleaned and dropped
    kept, dropped = scrub(dict(args), "CPU on server02 over the last hour")
    assert kept.get("history_minutes") == 60 and not dropped


def test_zammad_filtered_question_skips_the_estate_download(monkeypatch):
    """"Tickets created today" paged all 4,794 tickets (36s, 5.7 MB) to find 0.
    Filters go to Zammad's search; too many matches (None) or a failed search
    must still fall back to the estate rather than undercount."""
    from datetime import datetime
    from tools import zammad_tool
    from tools.zammad_tool import query_zammad, IST, _window

    now = datetime.now(IST).isoformat()
    calls = {"estate": 0, "queries": []}

    class Fake:
        SEARCH_CAP = 200
        hits = []
        def search_all(self, query):
            calls["queries"].append(query)
            if isinstance(self.hits, Exception):
                raise self.hits
            return self.hits
        def all_tickets(self):
            calls["estate"] += 1
            return [{"id": 1, "created_at": now, "state_id": 1}]
        def state_map(self): return {1: "open", 2: "closed"}
        def __getattr__(self, name): return lambda *a, **k: []

    monkeypatch.setattr(zammad_tool, "ZammadClient", Fake)

    Fake.hits = []
    result = query_zammad.invoke({"date_filter": "today", "state_filter": "open"})
    assert calls["estate"] == 0 and "FILTERED RESULT: 0" in result["summary"]
    assert 'state.name:"open"' in calls["queries"][-1] and " AND " in calls["queries"][-1]

    Fake.hits = [{"id": 7, "created_at": now, "state_id": 1},
                 {"id": 8, "created_at": now, "state_id": 2}]
    result = query_zammad.invoke({"state_filter": "open"})
    assert calls["estate"] == 0 and len(result["dataframe"]) == 1, "pandas re-check dropped"
    assert "not the whole helpdesk" in result["summary"]
    assert result["summary"].count("state in") == 2, "label once in scope, once in result"

    Fake.hits = None
    query_zammad.invoke({"date_filter": "today"})
    assert calls["estate"] == 1, "too many matches must fall back to the estate"

    Fake.hits = RuntimeError("search down")
    result = query_zammad.invoke({"date_filter": "today"})
    assert calls["estate"] == 2 and len(result["raw"]["result"]) == 1

    # 00:10 IST is still the previous day in UTC; "today" must start at IST midnight.
    start, end = _window("today", datetime(2026, 9, 14, 0, 10, tzinfo=IST))
    assert start == datetime(2026, 9, 14, tzinfo=IST) and end is None


def test_zammad_search_all_pages_past_the_cap():
    """Search stops at 200 a page; paging reaches the rest, and past max_rows
    the caller is told to use the estate instead."""
    from tools.clients import ZammadClient

    client = ZammadClient.__new__(ZammadClient)
    rows = [{"id": i} for i in range(450)]
    client.search = lambda q, limit, strict, page: rows[(page - 1) * 200:page * 200]
    client.get = lambda path, params: {"total_count": len(rows)}
    assert len(client.search_all("x")) == 450
    assert client.search_all("x", max_rows=400) is None
    rows = rows[:5]
    assert len(client.search_all("x")) == 5


def test_ceph_token_is_never_shared_across_credentials(monkeypatch):
    """One global token handed the first user's Ceph login to everyone for ~8h."""
    from tools import clients

    logins = []

    class Response:
        status_code = 200
        text = ""
        def __init__(self, user): self.user = user
        def json(self): return {"token": f"token-for-{self.user}"}

    def fake_request(method, url, **kwargs):
        logins.append(kwargs["json"]["username"])
        return Response(kwargs["json"]["username"])

    monkeypatch.setattr(clients, "request", fake_request)
    monkeypatch.setattr(clients, "_CEPH_TOKENS", {})

    def client(user, password):
        c = clients.CephClient.__new__(clients.CephClient)
        c.url, c.user, c.password, c.verify = "https://ceph", user, password, False
        return c

    assert client("alice", "a-pass")._token() == "token-for-alice"
    assert client("bob", "b-pass")._token() == "token-for-bob"
    assert client("alice", "a-pass")._token() == "token-for-alice"
    assert logins == ["alice", "bob"], "same credentials reuse one login, others get their own"
    client("alice", "wrong")._token()
    assert logins[-1] == "alice" and len(logins) == 3, "a different password is a different login"


def test_deployment_credentials_never_reach_a_signed_in_user(monkeypatch):
    """PROMETHEUS_TOKEN and the Ceph container-log cluster used to come from .env
    for every user."""
    from agent import auth_store, config_store
    from tools.clients import CephDaemonLogClient
    monkeypatch.setenv("PROMETHEUS_TOKEN", "deployment-token")
    monkeypatch.setenv("OPENSEARCH2_URL", "https://shared-cluster:9200")
    with _fresh_db() as (db, _):
        a = auth_store.signup("alice", "secret123", "a@example.com")["user_id"]
        config_store.user_save(a, {"PROMETHEUS_URL": "http://prom-a",
                                   "PROMETHEUS_TOKEN": "alice-token", "CEPH_URL": "https://ceph-a"})
        try:
            config_store.use_user(a)
            assert config_store.get("PROMETHEUS_TOKEN") == "alice-token"
            with pytest.raises(config_store.NotConnected):
                CephDaemonLogClient()
        finally:
            config_store.use_user(None)


def test_feedback_is_per_user_and_updates_in_place():
    from fastapi.testclient import TestClient
    import main
    with _fresh_db() as (db, _):
        alice, bob = TestClient(main.app), TestClient(main.app)
        alice.post("/auth/signup", json={"username": "alice", "password": "secret123",
                                         "email": "a@example.com"})
        bob.post("/auth/signup", json={"username": "bob", "password": "secret456",
                                       "email": "b@example.com"})
        a = db.find_user("alice")["id"]
        cid = db.new_conversation_id()
        turn = db.save_turn(a, cid, "how many hosts", "11", {"tools_called": ["query_zabbix"]})

        assert bob.post("/feedback", json={"turn_id": turn, "rating": -1}).status_code == 404
        assert alice.post("/feedback", json={"turn_id": turn, "rating": 2}).status_code == 400
        assert alice.post("/feedback", json={"turn_id": turn, "rating": 1}).status_code == 200
        assert alice.post("/feedback", json={"turn_id": turn, "rating": -1}).status_code == 200
        assert db.load_conversation(a, cid)[0]["rating"] == -1, "re-rating updates in place"
        assert alice.get("/admin/feedback").json()["downvotes"][0]["tools_called"] == ["query_zabbix"]
        assert bob.get("/admin/feedback").status_code == 403

        db.delete_conversation(a, cid)
        with db.tx() as c:
            assert c.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 0


def test_changes_before_problems_splits_people_from_zabbix_and_admits_blind_spots():
    """A person's change inside the 24h before a problem is the finding; Zabbix's
    own churn is counted, not listed; a host with no audit trail is 'cannot rule
    out', never 'nothing changed'."""
    from tools.zabbix.changes import query_changes

    T = 1_800_000_000
    audit = {"10": [
        {"resourceid": "10", "resourcetype": "4", "action": "1", "clock": str(T - 600),
         "username": "Admin", "resourcename": "r1"},
        {"resourceid": "10", "resourcetype": "4", "action": "1", "clock": str(T - 1200),
         "username": "System", "resourcename": "r1"},
        {"resourceid": "10", "resourcetype": "4", "action": "1", "clock": str(T - 3 * 86400),
         "username": "Admin", "resourcename": "r1"},
    ], "20": []}
    hosts = {"r1": "10", "r2": "20"}

    def rpc(method, params):
        if method == "problem.get":
            return [{"eventid": "1", "objectid": "t1", "clock": str(T), "name": "down",
                     "severity": "4", "tags": []},
                    {"eventid": "2", "objectid": "t2", "clock": str(T), "name": "slow",
                     "severity": "2", "tags": []}]
        if method == "trigger.get" and "triggerids" in params:
            return [{"triggerid": "t1", "hosts": [{"name": "r1"}]},
                    {"triggerid": "t2", "hosts": [{"name": "r2"}]}]
        if method == "host.get":
            name = params["search"]["name"]
            return [{"hostid": hosts[name], "host": name, "name": name}]
        if method in ("item.get", "trigger.get"):
            return []
        if method == "auditlog.get":
            rid = params["filter"]["resourceid"][0]
            rows = [e for e in audit[rid]
                    if params.get("time_from", 0) <= int(e["clock"]) <= params.get("time_till", 10**12)]
            return [dict(e) for e in rows][:params["limit"]]
        raise AssertionError(method)

    result = query_changes(rpc)
    df, summary = result["dataframe"], result["summary"]
    assert list(df["by"]) == ["Admin", "System"], "the 3-day-old change is outside the window"
    assert df.iloc[0]["minutes_before"] == 10
    assert df.iloc[1]["change"] == "1 automatic change(s)"
    assert "BY A PERSON" in summary
    assert "No audit trail exists at all for r2" in summary and "cannot be ruled out" in summary


# --- ticket assist: the only write this app makes ----------------------------
_ZABBIX_BODY = ("Problem started at 10:45:28 on 2026.09.21 \n"
                "Problem name: Linux: Nikunj VM has been restarted (uptime < 10m) \n"
                "Host: Nikunj VM \nSeverity: Warning \nOperational data: 00:02:26 \n"
                "Original problem ID: 3064125 \n"
                "http://192.168.1.180/zabbix.php/tr_events.php?triggerid=32864&eventid=3064125")


class _FakeZammad:
    """Stands in for ZammadClient and records every write."""

    def __init__(self, body=_ZABBIX_BODY, tags=()):
        self.body, self.tags, self.writes = body, list(tags), []

    def ticket(self, ticket_id):
        return {"id": ticket_id, "number": "905345"}

    def articles(self, ticket_id):
        return [{"body": self.body}]

    def ticket_tags(self, ticket_id):
        return self.tags

    def add_tag(self, ticket_id, tag):
        self.writes.append(("tag", ticket_id, tag))

    def add_note(self, ticket_id, body):
        self.writes.append(("note", ticket_id, body))


def _assist(mode, client, ticket_id=5447, **env):
    """Run ticket_assist.handle with the model and the log file faked out."""
    import os
    import tempfile
    from pathlib import Path
    from agent import ticket_assist
    settings = {"TICKET_ASSIST_MODE": mode, **env}
    saved = {key: os.environ.get(key) for key in settings}
    original_fix, original_log = ticket_assist.suggest_fix, ticket_assist.LOG_PATH
    os.environ.update(settings)
    ticket_assist.suggest_fix = lambda parsed, body: "- check the uptime"
    with tempfile.TemporaryDirectory() as folder:
        ticket_assist.LOG_PATH = Path(folder) / "log.jsonl"
        try:
            return ticket_assist.handle(ticket_id, client)
        finally:
            ticket_assist.suggest_fix, ticket_assist.LOG_PATH = original_fix, original_log
            for key, value in saved.items():
                os.environ.pop(key, None) if value is None else os.environ.update({key: value})


def test_ticket_assist_is_off_unless_switched_on():
    """It ships disabled: a webhook arriving before anyone opted in does nothing."""
    from agent import ticket_assist
    ticket_assist._reset()
    client = _FakeZammad()
    for mode in ("off", "", "WRITE-ISH"):
        assert _assist(mode, client)["outcome"] == "skipped"
    assert client.writes == []


def test_ticket_assist_dry_run_never_writes():
    from agent import ticket_assist
    ticket_assist._reset()
    client = _FakeZammad()
    record = _assist("dry_run", client)
    assert record["outcome"] == "dry_run" and "- check the uptime" in record["suggestion"]
    assert record["suggestion"].startswith(ticket_assist.HEADER)
    assert client.writes == [], "dry run must not touch Zammad"


def test_ticket_assist_writes_one_marked_note():
    from agent import ticket_assist
    ticket_assist._reset()
    client = _FakeZammad()
    assert _assist("write", client)["outcome"] == "written"
    assert [w[0] for w in client.writes] == ["tag", "note"], (
        "tag first: a failed second call must leave a mark, never an unmarked note")
    assert client.writes[0][2] == ticket_assist.TAG
    # Zammad delivering the same ticket twice must not produce a second note.
    assert _assist("write", client)["outcome"] == "skipped"
    assert len(client.writes) == 2


def test_ticket_assist_waits_out_a_new_tickets_first_minutes():
    """Triggers nobody here owns fire on any change to a ticket under a minute old."""
    import time
    from datetime import datetime, timedelta, timezone
    from agent import ticket_assist
    slept, original = [], time.sleep
    time.sleep = slept.append
    try:
        now = datetime.now(timezone.utc)
        ticket_assist._wait_until_settled({"created_at": now.isoformat().replace("+00:00", "Z")})
        ticket_assist._wait_until_settled({"created_at": (now - timedelta(hours=1)).isoformat()})
        ticket_assist._wait_until_settled({})
    finally:
        time.sleep = original
    assert len(slept) == 1 and 80 < slept[0] <= 90, slept


def test_ticket_assist_skips_what_it_should_not_touch():
    """A person's ticket, and one that already carries a suggestion."""
    from agent import ticket_assist
    for client, reason in ((_FakeZammad(body="Need a new laptop before October"), "not a Zabbix"),
                           (_FakeZammad(tags=[ticket_assist.TAG]), "already has")):
        ticket_assist._reset()
        record = _assist("write", client)
        assert record["outcome"] == "skipped" and reason in record["reason"], record
        assert client.writes == []


def test_ticket_assist_stops_at_the_daily_limit():
    """An alert storm raised 67 tickets in a day against a per-minute token cap."""
    from agent import ticket_assist
    ticket_assist._reset()
    client = _FakeZammad()
    outcomes = [_assist("write", client, ticket_id=n, TICKET_ASSIST_DAILY_LIMIT="2")["outcome"]
                for n in (1, 2, 3)]
    assert outcomes == ["written", "written", "skipped"]


def test_ticket_assist_records_a_failure_instead_of_raising():
    """It runs as a background task, where an exception would vanish."""
    from agent import ticket_assist
    ticket_assist._reset()

    class _Broken(_FakeZammad):
        def add_note(self, ticket_id, body):
            raise RuntimeError("ZAMMAD_WRITE_TOKEN is not set")

    record = _assist("write", _Broken())
    assert record["outcome"] == "error" and "ZAMMAD_WRITE_TOKEN" in record["error"]


def test_webhook_signature_is_required():
    """The API has no other authentication, and this call spends tokens and writes."""
    import os
    import hmac
    import hashlib
    from agent import ticket_assist
    body = b'{"ticket": {"id": 5447}}'
    saved = os.environ.get("ZAMMAD_WEBHOOK_SECRET")
    try:
        os.environ.pop("ZAMMAD_WEBHOOK_SECRET", None)
        assert not ticket_assist.signature_ok(body, "sha1=anything"), "no secret means refuse"
        os.environ["ZAMMAD_WEBHOOK_SECRET"] = "s3cret"
        good = hmac.new(b"s3cret", body, hashlib.sha1).hexdigest()
        assert ticket_assist.signature_ok(body, f"sha1={good}")
        assert ticket_assist.signature_ok(body, good)
        assert not ticket_assist.signature_ok(body, "")
        assert not ticket_assist.signature_ok(body, "sha1=" + "0" * 40)
        assert not ticket_assist.signature_ok(body + b" ", f"sha1={good}"), "a changed body must fail"
    finally:
        os.environ.pop("ZAMMAD_WEBHOOK_SECRET", None) if saved is None else os.environ.update(
            {"ZAMMAD_WEBHOOK_SECRET": saved})


def test_zammad_writes_use_their_own_token_and_are_never_retried():
    """A note POST that times out may already be stored; sending it again posts it twice."""
    import os
    import requests
    from tools import clients
    saved = os.environ.get("ZAMMAD_WRITE_TOKEN")
    try:
        os.environ.pop("ZAMMAD_WRITE_TOKEN", None)
        try:
            clients.ZammadClient(service_token="t").add_note(1, "x")
            assert False, "writing without ZAMMAD_WRITE_TOKEN must refuse"
        except RuntimeError:
            pass
        os.environ["ZAMMAD_WRITE_TOKEN"] = "write-token"
        fake = _Flaky(requests.exceptions.ReadTimeout("slow"), failures=99)
        try:
            _with_fake(fake, lambda: clients.ZammadClient(service_token="t").add_note(1, "x"))
        except requests.exceptions.ReadTimeout:
            pass
        assert fake.calls == 1, f"a write must be sent once, got {fake.calls} attempts"
    finally:
        os.environ.pop("ZAMMAD_WRITE_TOKEN", None) if saved is None else os.environ.update(
            {"ZAMMAD_WRITE_TOKEN": saved})


# --- dashboard: figures straight from each system, no model ------------------
def test_dashboard_tiles_fail_one_at_a_time_and_are_cached(monkeypatch):
    """One system being down or unconnected must blank its own tile, not the page."""
    from fastapi.testclient import TestClient
    import main
    from agent import config_store, dashboard
    calls, seen_users = [], []

    def healthy():
        calls.append("zabbix")
        seen_users.append(config_store.current_user_id())
        return {"hosts": {"total": 3}}

    def unconnected():
        raise config_store.NotConnected("Zammad isn't connected for your account")

    def broken():
        raise RuntimeError("API token expired.")

    monkeypatch.setitem(dashboard.SOURCES, "zabbix", healthy)
    monkeypatch.setitem(dashboard.SOURCES, "zammad", unconnected)
    monkeypatch.setitem(dashboard.SOURCES, "ceph", broken)
    with _fresh_db():
        client = TestClient(main.app)
        assert client.get("/dashboard/zabbix").status_code == 401, "needs a session"
        assert client.post("/auth/signup", json={"username": "nikunj", "password": "secret123",
                                                 "email": "n@example.com"}).json()["ok"]

        ok = client.get("/dashboard/zabbix").json()
        assert ok["status"] == "ok" and ok["data"] == {"hosts": {"total": 3}}
        assert seen_users and seen_users[0] is not None, "the source must run as the signed-in user"
        assert client.get("/dashboard/zammad").json()["status"] == "not_connected"
        failed = client.get("/dashboard/ceph").json()
        assert failed["status"] == "error" and "API token expired" in failed["message"]
        assert client.get("/dashboard/nonsense").status_code == 404

        # A second look inside the minute costs the source system nothing...
        client.get("/dashboard/zabbix")
        assert calls == ["zabbix"]
        # ...and the Refresh button goes around the cache.
        client.get("/dashboard/zabbix?fresh=true")
        assert calls == ["zabbix", "zabbix"]
        dashboard.Cached.clear_all()


def test_dashboard_zabbix_counts_only_monitored_hosts(monkeypatch):
    """Disabled hosts are not "unknown"; they are not monitored at all."""
    from agent import dashboard

    class _Zabbix:
        def hosts(self):
            up = {"status": "0", "name": "sw1", "active_available": "0",
                  "interfaces": [{"available": "1"}]}
            down = {"status": "0", "name": "r1", "active_available": "0",
                    "interfaces": [{"available": "2"}]}
            unreachable = {"status": "0", "name": "r2", "active_available": "0",
                           "interfaces": [{"available": "1", "error": "timeout"}]}
            disabled = {"status": "1", "name": "old", "interfaces": []}
            return [up, down, unreachable, disabled]

        def problems(self, limit=500):
            return [{"severity": "4", "acknowledged": "0"}, {"severity": "4", "acknowledged": "1"},
                    {"severity": "2", "acknowledged": "0"}]

    monkeypatch.setattr(dashboard, "ZabbixClient", _Zabbix)
    data = dashboard.zabbix()
    assert data["hosts"] == {"total": 3, "up": 1, "down": 2, "unknown": 0,
                             "down_names": ["r1", "r2"]}
    counts = {row["severity"]: row["count"] for row in data["problems"]["by_severity"]}
    assert counts["High"] == 2 and counts["Warning"] == 1 and counts["Disaster"] == 0
    assert data["problems"]["total"] == 3 and data["problems"]["unacknowledged"] == 2
