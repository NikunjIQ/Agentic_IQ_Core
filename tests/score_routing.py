#!/usr/bin/env python
"""Score the classify node: how often does it pick the right system?

    python tests/score_routing.py                  # everything, ~10 min
    python tests/score_routing.py --only ceph      # one system, cheap
    python tests/score_routing.py --limit 10       # first N cases
    python tests/score_routing.py --no-stability   # skip the repeat pass

Spends tokens: one classify call per question, ~1,500 each, so a full run is
around 110,000. Two caps apply, and both bite here:

  per minute  ~8,000 tokens. IQCORE_EVAL_PACING spaces the calls out; without
              it the run rate-limits and the failures read as routing bugs.
  per DAY     200,000 tokens on gpt-oss-20b. Two full runs exhaust it, and
              then nothing else can call a model until it resets. Use --only
              and --limit; score one group at a time.

A rate limit stops the run and reports whatever was measured, rather than
losing the whole thing to the last question.

No tools are called. This measures the routing decision only.
"""

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import conftest  # noqa: F401,E402  (path + env bootstrap)

import routing_cases  # noqa: E402
from agent import telemetry  # noqa: E402
from agent.nodes.classify import classify_node  # noqa: E402

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
PACING = float(os.getenv("IQCORE_EVAL_PACING", "8"))


def classify(question: str) -> dict:
    """One routing decision, with what it cost."""
    telemetry.start()
    state = classify_node({"question": question, "conversation_history": []})
    calls = telemetry.get().calls
    return {
        "tool": state.get("simple_tool_choice"),
        "complexity": state.get("complexity"),
        "asked": bool(state.get("clarify_axis")),
        "tokens": sum(c["total_tokens"] for c in calls),
    }


def _system(tool: str) -> str:
    return (tool or "-").replace("query_", "")


class BudgetSpent(Exception):
    """The day's tokens are gone. Whatever was scored still counts."""


def run(cases, label, pace=True):
    """Score cases of the form (question, expected_tool, expected_complexity)."""
    rows = []
    for index, (question, expected_tool, expected_complexity) in enumerate(cases):
        try:
            result = classify(question)
        except Exception as exc:
            # gpt-oss-20b has a 200,000 tokens-per-DAY ceiling as well as the
            # per-minute one, and a full scoring run is ~110,000 of it. Losing
            # every measured row to the last question would make the expensive
            # part of the run worthless.
            if "rate_limit" not in str(exc) and "429" not in str(exc):
                raise
            print(f"  {YELLOW}stopped after {len(rows)} of {len(cases)}"
                  f" — {str(exc)[:120]}{RESET}")
            raise BudgetSpent(str(exc)) from None
        # A cross-system case names no tool: the orchestrator picks its own, so
        # only the complexity judgement is this node's to get right.
        tool_ok = expected_tool is None or result["tool"] == expected_tool
        rows.append({
            "question": question,
            "expected": expected_tool,
            "got": result["tool"],
            "tool_ok": tool_ok,
            "complexity_ok": result["complexity"] == expected_complexity,
            "asked": result["asked"],
            "tokens": result["tokens"],
        })
        if pace and index < len(cases) - 1:
            time.sleep(PACING)
    return rows


def report(rows, label):
    total = len(rows)
    if not total:
        return 0, 0
    tool_ok = sum(r["tool_ok"] for r in rows)
    cx_ok = sum(r["complexity_ok"] for r in rows)
    asked = sum(r["asked"] for r in rows)
    tokens = sum(r["tokens"] for r in rows)

    colour = GREEN if tool_ok == total else (YELLOW if tool_ok >= total * 0.8 else RED)
    print(f"\n{label}")
    print(f"  right system      {colour}{tool_ok}/{total}{RESET}"
          f"  ({tool_ok / total * 100:.0f}%)")
    print(f"  right complexity  {cx_ok}/{total}  ({cx_ok / total * 100:.0f}%)")
    print(f"  asked instead     {asked}/{total}"
          + (f"  {RED}<- should have routed{RESET}" if asked else ""))
    print(f"  tokens            {tokens:,} total, {tokens // total:,} per decision")

    for r in rows:
        if not r["tool_ok"]:
            print(f"    {RED}wrong{RESET} {r['question'][:52]:<54}"
                  f" wanted {_system(r['expected'])}, got {_system(r['got'])}"
                  + ("  (asked)" if r["asked"] else ""))
    return tool_ok, total


def report_collisions(rows_by_question):
    """Homonyms are scored apart: 'defensible' is not the same as 'correct'."""
    print(f"\ncollisions {DIM}(two systems own the same word){RESET}")
    exact = defensible = 0
    for question, accepted, preferred in routing_cases.COLLISION_CASES:
        got = rows_by_question.get(question)
        if got is None:
            continue
        if got == preferred:
            exact += 1
            mark = f"{GREEN}preferred{RESET}"
        elif got in accepted:
            defensible += 1
            mark = f"{YELLOW}defensible{RESET}"
        else:
            mark = f"{RED}wrong{RESET}"
        print(f"  {mark:<22} {question[:46]:<48} -> {_system(got)}"
              f" {DIM}(prefer {_system(preferred)}){RESET}")
    total = len(routing_cases.COLLISION_CASES)
    print(f"  {exact} preferred, {defensible} defensible, "
          f"{total - exact - defensible} wrong, of {total}")


def report_should_ask(results):
    print(f"\nvague questions {DIM}(a question back is the right answer){RESET}")
    asked = sum(1 for r in results.values() if r["asked"])
    for question, result in results.items():
        mark = f"{GREEN}asked{RESET}" if result["asked"] else f"{RED}answered{RESET}"
        print(f"  {mark:<18} {question[:46]:<48} -> {_system(result['tool'])}")
    print(f"  {asked}/{len(results)} correctly asked")


def report_stability(pairs):
    """The same question routed differently on two consecutive turns once."""
    moved = [q for q, (a, b) in pairs.items() if a != b]
    print(f"\nstability {DIM}(same question, asked twice){RESET}")
    print(f"  {len(pairs) - len(moved)}/{len(pairs)} gave the same answer both times")
    for question in moved:
        first, second = pairs[question]
        print(f"    {YELLOW}moved{RESET} {question[:46]:<48}"
              f" {_system(first)} -> {_system(second)}")


def main():
    parser = argparse.ArgumentParser(description="Score the classify node")
    parser.add_argument("--only", default="",
                        help="one group: ceph, ceph_daemon, zabbix, zammad, prometheus, "
                             "opensearch, cross, collisions, ask")
    parser.add_argument("--limit", type=int, default=0, help="first N cases only")
    parser.add_argument("--no-stability", action="store_true")
    args = parser.parse_args()

    groups = {
        "ceph": routing_cases.CEPH_ROUTING,
        "ceph_daemon": routing_cases.CEPH_DAEMON_ROUTING,
        "zabbix": routing_cases.ZABBIX_ROUTING,
        "zammad": routing_cases.ZAMMAD_ROUTING,
        "prometheus": routing_cases.PROMETHEUS_ROUTING,
        "opensearch": routing_cases.OPENSEARCH_ROUTING,
        "cross": routing_cases.CROSS_SYSTEM_ROUTING,
    }
    if args.only and args.only not in groups and args.only not in ("collisions", "ask"):
        print(f"unknown group {args.only!r}")
        return 2

    selected = ({args.only: groups[args.only]} if args.only in groups
                else ({} if args.only else groups))

    started = time.time()
    grand_ok = grand_total = 0
    spent = False
    for name, cases in selected.items():
        if args.limit:
            cases = cases[:args.limit]
        try:
            rows = run(cases, name)
        except BudgetSpent:
            spent = True
            break
        ok, total = report(rows, f"{name} {DIM}({total_hint(cases)}){RESET}")
        grand_ok += ok
        grand_total += total

    if not spent and (not args.only or args.only == "collisions"):
        cases = [(q, None, None) for q, _, _ in routing_cases.COLLISION_CASES]
        rows = run(cases, "collisions")
        report_collisions({r["question"]: r["got"] for r in rows})

    if not spent and (not args.only or args.only == "ask"):
        results = {}
        for index, question in enumerate(routing_cases.SHOULD_ASK):
            results[question] = classify(question)
            if index < len(routing_cases.SHOULD_ASK) - 1:
                time.sleep(PACING)
        report_should_ask(results)

    if not spent and not args.only and not args.no_stability:
        pairs = {}
        for index, question in enumerate(routing_cases.STABILITY_SAMPLE):
            first = classify(question)["tool"]
            time.sleep(PACING)
            second = classify(question)["tool"]
            pairs[question] = (first, second)
            if index < len(routing_cases.STABILITY_SAMPLE) - 1:
                time.sleep(PACING)
        report_stability(pairs)

    print("\n" + "=" * 62)
    if spent:
        print(f"{YELLOW}stopped early — the day's token budget is gone."
              f" Scores above cover only what completed.{RESET}")
    if grand_total:
        colour = GREEN if grand_ok == grand_total else RED
        print(f"routing accuracy {colour}{grand_ok}/{grand_total}{RESET}"
              f"  ({grand_ok / grand_total * 100:.1f}%)"
              f"  {DIM}({time.time() - started:.0f}s){RESET}")
    return 0


def total_hint(cases) -> str:
    return f"{len(cases)} questions"


if __name__ == "__main__":
    sys.exit(main())
