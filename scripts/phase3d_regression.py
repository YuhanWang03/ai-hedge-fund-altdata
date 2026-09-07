"""Authenticated production regression for the Research V1.0 RC set."""

from __future__ import annotations

import json
import os
import time
import urllib.request


BASE = os.environ.get("RESEARCH_API_BASE", "http://127.0.0.1:8100")
TOKEN = os.environ.get("WEB_OWNER_TOKEN", "")
FORCE_REFRESH = os.environ.get("RESEARCH_FORCE_REFRESH", "true").strip().lower() not in {"0", "false", "no"}
DEFAULT_TICKERS = ("AAPL", "NVDA", "MSFT", "JPM", "BAC", "XOM", "CVX", "TSLA", "GM", "F", "WMT", "KO", "CAT")
TICKERS = tuple(filter(None, (part.strip().upper() for part in os.environ.get("RESEARCH_TICKERS", ",".join(DEFAULT_TICKERS)).split(","))))
EXPECTED = {**{key: "TECHNOLOGY" for key in ("AAPL", "NVDA", "MSFT")},
            **{key: "FINANCIALS" for key in ("JPM", "BAC")}, **{key: "ENERGY" for key in ("XOM", "CVX")},
            **{key: "AUTOMOTIVE" for key in ("TSLA", "GM", "F")}, **{key: "GENERAL" for key in ("WMT", "KO", "CAT")}}


def request(path: str, body: dict | None = None) -> dict:
    payload = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=payload, method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json", "X-Owner-Token": TOKEN})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def summarize(result: dict) -> dict:
    modules = result.get("modules", {})
    industry = result.get("industry_metrics", {})
    debate = result.get("investment_debate", {})
    scenarios = result.get("scenarios", {})
    profile = result.get("industry_context", {}).get("profile")
    factuality = result.get("investment_thesis_v3", {}).get("factuality", {})
    return {
        "run_id": result.get("run_id"), "snapshot_id": result.get("snapshot_id"), "engine_version": result.get("engine_version"),
        "status": result.get("status"), "profile": profile, "profile_expected": EXPECTED[result["ticker"]], "profile_ok": profile == EXPECTED[result["ticker"]],
        "research_completeness": round(sum(float(row.get("completeness", 0) or 0) for row in modules.values()) / max(1, len(modules)), 3),
        "required_coverage": industry.get("required_coverage", industry.get("completeness")),
        "required_available": industry.get("available_count"), "required_total": industry.get("required_count"),
        "optional_coverage": industry.get("optional_coverage"),
        "optional_available": industry.get("optional_available_count"), "optional_total": industry.get("optional_count"),
        "research_support_tier": result.get("research_support_tier"),
        "industry_fallback": result.get("industry_fallback_detection", {}).get("status"),
        "ga_coverage_gate": result.get("ga_coverage_gate", {}).get("status"),
        "confidence": result.get("research_confidence", {}).get("score"), "quality_gate": result.get("quality_gate", {}).get("status"),
        "peer_relevance": [{"ticker": row.get("ticker"), "score": row.get("relevance_score"), "label": row.get("relevance_label")} for row in result.get("peer_relevance", [])],
        "core_thesis": result.get("core_thesis"),
        "main_tension": result.get("main_tension", {}).get("statement"),
        "bulls": [row.get("claim") for row in debate.get("what_bulls_believe", [])], "bears": [row.get("claim") for row in debate.get("what_bears_believe", [])],
        "matters": [row.get("claim") for row in debate.get("what_matters_most", [])],
        "scenarios_ok": all(scenarios.get(name, {}).get("narrative") for name in ("BULL", "BASE", "BEAR")),
        "risk_count": len(result.get("key_risks_v2", [])), "catalyst_count": len(result.get("key_catalysts", [])),
        "evidence_count": len(result.get("evidence_index", [])), "factuality": factuality.get("status"),
        "unsupported_financial_claims": len(factuality.get("unsupported_numbers", [])),
        "module_cache_hits": sum(bool(row.get("cache_hit")) for row in modules.values()),
        "provider_errors": sorted({error.get("type") for row in modules.values() for error in row.get("provider_errors", []) if error.get("type")}),
    }


def main() -> None:
    if not TOKEN:
        raise SystemExit("WEB_OWNER_TOKEN is not configured")
    completed: dict[str, dict] = {}
    for offset in range(0, len(TICKERS), 2):
        active = {ticker: request("/api/research/runs", {"ticker": ticker, "force_refresh": FORCE_REFRESH})["job_id"] for ticker in TICKERS[offset:offset + 2]}
        deadline = time.time() + 1800
        while active and time.time() < deadline:
            time.sleep(3)
            for ticker, run_id in list(active.items()):
                job = request(f"/api/research/runs/{run_id}")
                if job["status"] in {"COMPLETED", "PARTIAL_DATA", "PARTIAL_ERROR", "FAILED"}:
                    completed[ticker] = summarize(job["result"])
                    del active[ticker]
        if active:
            raise SystemExit(f"Timed out jobs: {active}")
    health = request("/api/research/health/providers")
    acceptance = {"all_completed_or_gracefully_limited": len(completed) == len(TICKERS) and all(
                      row["status"] in {"COMPLETED", "PARTIAL_DATA", "PARTIAL_ERROR"}
                      and row["ga_coverage_gate"] in {"PASSED", "LIMITED"}
                      and row["research_support_tier"] != "INSUFFICIENT" for row in completed.values()),
                  "profiles_correct": all(row["profile_ok"] for row in completed.values()),
                  "factuality_passed": all(row["factuality"] == "PASSED" for row in completed.values()),
                  "no_unsupported_financial_claims": all(row["unsupported_financial_claims"] == 0 for row in completed.values()),
                  "scenarios_present": all(row["scenarios_ok"] for row in completed.values()),
                  "providers_healthy": all(row["status"] == "HEALTHY" for row in health["providers"]),
                  "coverage_gate_publishable": all(row["ga_coverage_gate"] in {"PASSED", "LIMITED"} for row in completed.values())}
    print(json.dumps({"acceptance": acceptance, "providers": health["providers"], "results": completed}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
