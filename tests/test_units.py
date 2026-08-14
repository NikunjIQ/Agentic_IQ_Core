"""Offline tests: wiring, the answer cache, and conversation history.

No network and no LLM, so these run in milliseconds and cost nothing. They are
the tests that should never be skipped.
"""

import time

import conftest  # noqa: F401  (path + env bootstrap)


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
    assert systems == {"Zabbix", "Zammad", "Prometheus", "OpenSearch", "Groq"}
    for key in ("ZABBIX_API_TOKEN", "ZAMMAD_PASS", "PROMETHEUS_PASS",
                "OPENSEARCH_PASS", "GROQ_API_KEY"):
        assert key in config_store.SECRET_KEYS, f"{key} must be treated as a secret"


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
