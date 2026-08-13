"""Offline tests: wiring, the answer cache, and conversation history.

No network and no LLM, so these run in milliseconds and cost nothing. They are
the tests that should never be skipped.
"""

import time

import conftest  # noqa: F401  (path + env bootstrap)


# --- wiring -----------------------------------------------------------------
def test_all_five_tools_are_registered():
    from agent.tool_registry import TOOL_REGISTRY
    expected = {"query_zabbix", "query_zammad", "query_prometheus",
                "correlate_problems_and_tickets", "lookup_ticket_context"}
    assert expected <= set(TOOL_REGISTRY), f"missing: {expected - set(TOOL_REGISTRY)}"


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


def test_cache_reports_stats():
    from agent import answer_cache
    answer_cache.clear()
    answer_cache.put("q", {"final_answer": "a", "complexity": "simple"}, [])
    answer_cache.get("q", [])
    answer_cache.get("never asked", [])
    stats = answer_cache.stats()
    assert stats["hits"] >= 1 and stats["misses"] >= 1
    assert 0.0 <= stats["hit_rate"] <= 1.0
