"""Replay recent Zabbix tickets through ticket assist and save the suggestions for review.

    python tests/replay_ticket_assist.py --limit 20

Writes nothing to Zammad. SPENDS TOKENS: one model call per distinct problem,
paced to stay under the per-minute cap. The output, tests/ticket_assist_review.csv,
has three empty score columns (1-5) for the team to fill in; the averages are
the baseline any later change, or another tool such as HolmesGPT run over the
same tickets, is compared against.
"""

import csv
import sys
import time
import argparse
from pathlib import Path

import conftest  # noqa: F401  (path + env bootstrap)

from agent import ticket_assist
from tools.correlation import parse_zabbix_ticket_body

OUT = Path(__file__).resolve().parent / "ticket_assist_review.csv"
SECONDS_BETWEEN_CALLS = 20  # ~1,500 tokens a call against 8,000 a minute


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=20, help="distinct problems to review")
    args = parser.parse_args()

    client = ticket_assist.service_client()
    tickets = sorted(client.search("*", limit=client.SEARCH_CAP),
                     key=lambda t: t.get("created_at", ""), reverse=True)
    if not tickets:
        sys.exit("No tickets came back from Zammad - check ZAMMAD_URL and ZAMMAD_WRITE_TOKEN.")

    rows, seen = [], set()
    for ticket in tickets:
        if len(rows) >= args.limit:
            break
        articles = client.articles(ticket["id"])
        body = (articles[0].get("body") or "") if articles else ""
        parsed = parse_zabbix_ticket_body(body)
        problem = parsed.get("problem_name")
        if not parsed.get("event_id") or not problem or problem in seen:
            continue
        seen.add(problem)
        if rows:
            time.sleep(SECONDS_BETWEEN_CALLS)
        try:
            suggestion = ticket_assist.suggest_fix(parsed, body)
        except Exception as exc:
            suggestion = f"ERROR: {type(exc).__name__}: {str(exc)[:200]}"
        print(f"[{len(rows) + 1}/{args.limit}] #{ticket.get('number')} {problem[:70]}")
        rows.append({"ticket": ticket.get("number"), "host": parsed.get("host"),
                     "severity": parsed.get("severity"), "problem": problem,
                     "suggestion": suggestion,
                     "useful_1to5": "", "correct_1to5": "", "safe_1to5": "", "comment": ""})

    if not rows:
        sys.exit("No Zabbix alert tickets found among the newest tickets.")
    with OUT.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"\n{len(rows)} suggestions saved to {OUT}")


if __name__ == "__main__":
    main()
