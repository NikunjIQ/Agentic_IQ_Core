#!/usr/bin/env python
"""Run the eval suite without needing pytest installed.

    python tests/run_evals.py              # offline + live tools (no token cost)
    python tests/run_evals.py --offline    # offline only, no network
    python tests/run_evals.py --llm        # everything, including extraction evals
    python tests/run_evals.py --routing    # score which system each question routes to
    python tests/run_evals.py -k traffic   # only tests whose name matches

Every test is a plain `test_*` function using bare asserts, so `pytest tests/`
works identically if pytest is ever added.
"""

import argparse
import importlib
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import conftest  # noqa: F401,E402  (path + env bootstrap)

SUITES = [
    ("offline  ", "test_units", False),
    ("live tool", "test_tools_live", False),
    ("llm eval ", "test_extraction_llm", True),
]

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def run_suite(label: str, module_name: str, pattern: str):
    module = importlib.import_module(module_name)
    tests = sorted(name for name in dir(module)
                   if name.startswith("test_") and callable(getattr(module, name)))
    if pattern:
        tests = [t for t in tests if pattern.lower() in t.lower()]

    passed, failed = 0, []
    for name in tests:
        started = time.time()
        try:
            getattr(module, name)()
            print(f"  {GREEN}PASS{RESET} {name} {DIM}({time.time() - started:.1f}s){RESET}")
            passed += 1
        except AssertionError as exc:
            print(f"  {RED}FAIL{RESET} {name} {DIM}({time.time() - started:.1f}s){RESET}")
            print(f"       {RED}{str(exc).strip()[:600]}{RESET}")
            failed.append((name, str(exc)))
        except Exception as exc:
            print(f"  {YELLOW}ERROR{RESET} {name}: {type(exc).__name__}: {str(exc)[:300]}")
            if "--traceback" in sys.argv:
                traceback.print_exc()
            failed.append((name, f"{type(exc).__name__}: {exc}"))
    return passed, failed


def main():
    parser = argparse.ArgumentParser(description="IQCore eval harness")
    parser.add_argument("--offline", action="store_true",
                        help="skip anything touching Zabbix/Zammad/Prometheus")
    parser.add_argument("--llm", action="store_true",
                        help="include extraction evals (spends Groq tokens)")
    parser.add_argument("-k", dest="pattern", default="",
                        help="only run tests whose name contains this")
    parser.add_argument("--traceback", action="store_true", help="full tracebacks")
    parser.add_argument("--routing", action="store_true",
                        help="score the classify node instead of running tests "
                             "(spends tokens; see tests/score_routing.py for "
                             "--only and --limit)")
    # Unknown arguments are kept rather than rejected so --routing can forward
    # --only and --limit to the scorer; a full routing run is ~93 model calls,
    # so narrowing it is the normal case, not an edge one.
    args, passthrough = parser.parse_known_args()

    if args.routing:
        # Routing is scored, not passed or failed: the useful output is a
        # percentage and the questions that went to the wrong system.
        import score_routing
        sys.argv = [sys.argv[0]] + passthrough
        return score_routing.main()
    if passthrough:
        parser.error(f"unrecognized arguments: {' '.join(passthrough)}")

    total_passed, total_failed = 0, []
    started = time.time()

    for label, module_name, needs_llm in SUITES:
        if needs_llm and not args.llm:
            print(f"\n{DIM}== {label} — skipped (pass --llm to include; spends tokens){RESET}")
            continue
        if args.offline and module_name != "test_units":
            print(f"\n{DIM}== {label} — skipped (--offline){RESET}")
            continue

        print(f"\n== {label} [{module_name}]")
        passed, failed = run_suite(label, module_name, args.pattern)
        total_passed += passed
        total_failed += failed

    elapsed = time.time() - started
    print("\n" + "=" * 62)
    if total_failed:
        print(f"{RED}{len(total_failed)} FAILED{RESET}, {total_passed} passed  "
              f"{DIM}({elapsed:.1f}s){RESET}")
        for name, message in total_failed:
            print(f"  - {name}: {message.strip()[:160]}")
        return 1
    print(f"{GREEN}All {total_passed} checks passed{RESET}  {DIM}({elapsed:.1f}s){RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
