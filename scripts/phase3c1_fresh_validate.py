"""Run and report five authenticated force-refresh Research jobs via backend."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request


BASE = os.environ.get("RESEARCH_API_BASE", "http://127.0.0.1:8100")
TOKEN = os.environ.get("WEB_OWNER_TOKEN", "")
TICKERS = ("AAPL", "NVDA", "JPM", "XOM", "TSLA")


def request(path: str, body: dict | None = None) -> dict:
    payload = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=payload, method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json", "X-Owner-Token": TOKEN})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def snapshot(ticker: str) -> dict | None:
    try:
        return request(f"/api/research/results/{ticker}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def summary(result: dict | None) -> dict:
    if not result:
        return {"exists": False}
    modules = result.get("modules", {})
    errors = sorted({error.get("type") for module in modules.values() for error in module.get("provider_errors", []) if error.get("type")})
    return {"exists": True, "run_id": result.get("run_id"), "snapshot_id": result.get("snapshot_id"),
            "engine_version": result.get("engine_version"), "generated_at": result.get("generated_at"),
            "confidence": result.get("research_confidence", {}).get("score"), "quality_gate": result.get("quality_gate", {}).get("status"),
            "module_completeness": {name: module.get("completeness") for name, module in modules.items()},
            "module_cache_hits": {name: bool(module.get("cache_hit")) for name, module in modules.items()},
            "provider_errors": errors, "raw_data_cache": result.get("production_diagnostics", {}).get("raw_data_cache", {})}


def main() -> None:
    if not TOKEN:
        raise SystemExit("WEB_OWNER_TOKEN is not configured for this validation process")
    before = {ticker: summary(snapshot(ticker)) for ticker in TICKERS}
    completed: dict[str, dict] = {}
    for offset in range(0, len(TICKERS), 2):
        active = {}
        for ticker in TICKERS[offset:offset + 2]:
            job = request("/api/research/runs", {"ticker": ticker, "force_refresh": True})
            active[ticker] = job["job_id"]
        deadline = time.time() + 1200
        while active and time.time() < deadline:
            time.sleep(3)
            for ticker, job_id in list(active.items()):
                job = request(f"/api/research/runs/{job_id}")
                if job["status"] in {"COMPLETED", "PARTIAL_DATA", "PARTIAL_ERROR", "FAILED"}:
                    completed[ticker] = job
                    del active[ticker]
        if active:
            raise SystemExit(f"Timed out jobs: {active}")
    after = {ticker: summary((completed[ticker].get("result") or snapshot(ticker))) for ticker in TICKERS}
    comparisons = {}
    for ticker in TICKERS:
        try:
            comparisons[ticker] = request(f"/api/research/history/{ticker}/compare")
        except urllib.error.HTTPError:
            comparisons[ticker] = None
    health = request("/api/research/health/providers")
    print(json.dumps({"before": before, "after": after, "provider_health": health["providers"], "comparisons": comparisons}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
