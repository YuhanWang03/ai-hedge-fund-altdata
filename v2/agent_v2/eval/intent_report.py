"""``python -m v2.agent_v2.eval.intent_report [--since DAYS] [--path FILE] [--json]``: the intent classifier's shadow agreement with the regex decision."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from v2.agent_v2.intent import agreement, ledger_path, read_shadow, render


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare the shadow intent classifier with the router and rule planner.")
    parser.add_argument("--since", type=int, default=None, help="only questions from the last N days")
    parser.add_argument("--path", type=Path, default=None, help=f"ledger file (default {ledger_path()})")
    parser.add_argument("--json", action="store_true", help="print the aggregates as JSON")
    args = parser.parse_args(argv)
    summary = agreement(read_shadow(args.path, since_days=args.since))
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(render(summary, since_days=args.since))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
