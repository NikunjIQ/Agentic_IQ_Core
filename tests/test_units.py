"""Offline tests: wiring, the answer cache, and conversation history.

No network, and no LLM except where SPENDS_TOKENS says otherwise, so these run
in milliseconds and cost nothing. They are the tests that should never be
skipped.
"""

import time

import conftest  # noqa: F401  (path + env bootstrap)

# This module is the free tier and must stay free. Anything named here calls
# a model, so run_evals holds it back unless --llm is passed.
# test_every_graph_node_announces_itself invokes the compiled graph, which
# runs classify and simple_path for real. It spent part of the daily budget
# on every "offline" run, and once that budget was gone it failed in a way
# that read as a routing regression rather than an exhausted quota.
SPENDS_TOKENS = {"test_every_graph_node_announces_itself"}


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
    assert systems == {"Zabbix", "Zammad", "Prometheus", "OpenSearch", "Ceph", "Groq"}
    for key in ("ZABBIX_API_TOKEN", "ZAMMAD_PASS", "PROMETHEUS_PASS",
                "OPENSEARCH_PASS", "CEPH_PASS", "GROQ_API_KEY"):
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
    assert "respond" in entered, f"respond never announced itself: {seen}"
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
