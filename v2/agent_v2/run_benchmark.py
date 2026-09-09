"""Run the ported V1 evaluation set against Agent V1 and Agent V2.

    python -m v2.agent_v2.run_benchmark                 # v1_baseline + v2_rules, dev set
    python -m v2.agent_v2.run_benchmark --holdout       # the 15 held-out questions
    python -m v2.agent_v2.run_benchmark --modes v2_llm --repeat 3   # needs a model key
    python -m v2.agent_v2.run_benchmark --fixtures engine   # engine-shaped research/market envelopes
    python -m v2.agent_v2.run_benchmark --json out.json
"""

from __future__ import annotations

import argparse
import json
import sys

from v2.agent_v2.eval.benchmark import MODES, gap_summary, render, run_benchmark, to_json
from v2.agent_v2.eval.benchmark_fixtures import FIXTURE_MODES
from v2.agent_v2.eval.benchmark_cases import DEV_CASES, HOLDOUT_CASES


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--modes", default="v1_baseline,v2_rules", help=f"comma-separated subset of {', '.join(MODES)}")
    parser.add_argument("--holdout", action="store_true", help="run the held-out set instead of the development set")
    parser.add_argument("--repeat", type=int, default=1, help="repeats per case for v2_llm (deterministic modes run once)")
    parser.add_argument("--json", dest="json_path", default="", help="write per-case scores to this file")
    parser.add_argument("--fixtures", default="v1", choices=FIXTURE_MODES, help="v1: V1 cards with V1 fact keys; engine: engine-shaped research/market envelopes")
    parser.add_argument("--no-failures", action="store_true", help="omit the per-mode failure list")
    args = parser.parse_args(argv)

    modes = tuple(mode.strip() for mode in args.modes.split(",") if mode.strip())
    unknown = [mode for mode in modes if mode not in MODES]
    if unknown:
        print(f"unknown mode(s): {', '.join(unknown)}", file=sys.stderr)
        return 2
    cases = HOLDOUT_CASES if args.holdout else DEV_CASES
    print(f"Agent V1→V2 benchmark · {'holdout' if args.holdout else 'dev'} set · {len(cases)} cases · modes={', '.join(modes)} · fixtures={args.fixtures}")
    if args.fixtures == "engine":
        from v2.agent_v2.eval.recorded import RecordedStore

        recorded = RecordedStore().summary()
        print("recorded envelopes: " + (", ".join(f"{name}={count}" for name, count in recorded.items()) if recorded else "none (offline synthesis only)"))
    gaps = gap_summary(cases)
    if gaps:
        print("capability gaps: " + "; ".join(f"{tool} → {len(ids)} case(s)" for tool, ids in gaps.items()))
    print()
    reports = run_benchmark(modes, holdout=args.holdout, repeat=args.repeat, fixtures=args.fixtures)
    print(render(reports, failures=not args.no_failures))
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump(to_json(reports), handle, ensure_ascii=False, indent=2)
        print(f"\nwrote {args.json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
