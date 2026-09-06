"""Print Phase 3C summaries from persisted, real Research snapshots."""

from __future__ import annotations

import argparse
import json

from v2.research.intelligence import build_intelligence
from v2.research.store import ResearchStore


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh-missing", action="store_true", help="run the full engine when no snapshot exists")
    args = parser.parse_args()
    store = ResearchStore()
    output = []
    for ticker in ("AAPL", "NVDA", "JPM", "XOM", "TSLA"):
        snapshot = store.latest(ticker)
        if not snapshot and args.refresh_missing:
            from v2.research.engine import ResearchEngine
            snapshot = ResearchEngine().run(ticker)
        if not snapshot:
            output.append({"ticker": ticker, "snapshot": False})
            continue
        modules = snapshot.get("modules", {})
        company = modules.get("fundamental", {}).get("details", {}).get("company", {})
        result = build_intelligence(ticker, modules, company)
        output.append({
            "ticker": ticker,
            "snapshot": True,
            "snapshot_generated_at": snapshot.get("generated_at"),
            "industry_profile": result["industry_context"]["profile"],
            "confidence": result["research_confidence"]["score"],
            "quality_gate": result["quality_gate"]["status"],
            "coverage": result["quality_gate"]["coverage"],
            "core_thesis": result["core_thesis"],
            "why_now": result["why_now"],
            "positive_drivers": [row["title"] for row in result["key_drivers"]["positive"]],
            "negative_drivers": [row["title"] for row in result["key_drivers"]["negative"]],
            "bull": result["scenarios"]["BULL"]["narrative"],
            "base": result["scenarios"]["BASE"]["narrative"],
            "bear": result["scenarios"]["BEAR"]["narrative"],
            "conflicts": [row["type"] for row in result["conflicts"]],
            "finding_count": len(result["research_findings"]),
            "verified_evidence_ratio": result["research_confidence"]["verified_evidence_ratio"],
        })
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
