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
    assert "OMIT THIS" in zammad, "date_filter guard missing; 8B invents time windows"
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
