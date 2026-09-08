"""Research Engine adapters with compact, evidence-preserving output."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from v2.agent_v2.execution import CapabilityRegistry, ExecutionContext
from v2.agent_v2.models import EvidenceItem, ResultStatus, ToolEnvelope

_FOCUS_MODULES = {
    "overview": ["fundamental", "valuation", "earnings", "risk"],
    "fundamentals": ["fundamental"],
    "valuation": ["valuation"],
    "earnings": ["earnings", "expectations", "sec"],
    "market": ["technical", "fund_flow"],
    "ownership": ["institutional"],
    "catalysts": ["catalyst"],
    "filings": ["sec"],
    "supply_chain": ["supply_chain"],
    "risk": ["risk"],
    "full": None,
}


def _engine_factory():
    from v2.research import ResearchEngine

    return ResearchEngine()


def _store_factory():
    from v2.research.store import ResearchStore

    return ResearchStore()


def _status(raw: str, cache_hit: bool) -> ResultStatus:
    if cache_hit:
        return ResultStatus.CACHED
    value = (raw or "").upper()
    if value == "COMPLETED":
        return ResultStatus.COMPLETED
    if value in {"PARTIAL", "PARTIAL_DATA"}:
        return ResultStatus.PARTIAL_DATA
    if value == "PARTIAL_ERROR":
        return ResultStatus.PARTIAL_ERROR
    return ResultStatus.FAILED


def _evidence(result: dict[str, Any]) -> list[EvidenceItem]:
    ticker = str(result.get("ticker") or "")
    run_id = str(result.get("run_id") or "")
    sources = {str(row.get("id")): row for row in result.get("sources", [])}
    items: list[EvidenceItem] = []
    for row in result.get("evidence_index", [])[:40]:
        source_ids = [str(value) for value in row.get("source_ids", []) if value]
        source = next((sources[value] for value in source_ids if value in sources), {})
        metrics = row.get("metrics") or {}
        metric = next(iter(metrics), "")
        items.append(
            EvidenceItem(
                id=str(row.get("id") or f"{run_id}-evidence-{len(items) + 1}"),
                entity=str(row.get("ticker") or ticker),
                claim=str(row.get("claim") or ""),
                metric=metric,
                value=metrics.get(metric) if metric else None,
                period=str(row.get("data_period") or ""),
                as_of=str(row.get("published_at") or row.get("fetched_at") or result.get("generated_at") or ""),
                source_id=source_ids[0] if source_ids else "",
                source_title=str(source.get("title") or ""),
                source_url=str(source.get("url") or ""),
                confidence=None,
                producer_run_id=run_id,
                metadata={"module": row.get("module"), "verified": bool(row.get("verified")), "metrics": metrics},
            )
        )
    return items


def _limitations(result: dict[str, Any]) -> list[str]:
    limitations = list(result.get("confidence_limitations") or [])
    diagnostics = result.get("production_diagnostics", {}).get("modules", {})
    for name, row in diagnostics.items():
        if row.get("status") in {"FAILED", "PARTIAL", "PARTIAL_DATA", "PARTIAL_ERROR"}:
            limitations.append(f"{name}: {row.get('status')}")
    return list(dict.fromkeys(str(value) for value in limitations if value))[:12]


def _envelope(result: dict[str, Any], capability: str) -> ToolEnvelope:
    findings = list(result.get("research_findings") or [])[:12]
    return ToolEnvelope(
        capability=capability,
        status=_status(str(result.get("status") or ""), bool(result.get("from_cache"))),
        subject=str(result.get("ticker") or ""),
        as_of=str(result.get("generated_at") or ""),
        summary=str(result.get("core_thesis") or result.get("investment_thesis") or ""),
        metrics={"scores": result.get("scores", {}), "risk_level": result.get("risk_level"), "confidence": result.get("research_confidence", {})},
        findings=findings,
        evidence=_evidence(result),
        limitations=_limitations(result),
        run_id=str(result.get("run_id") or ""),
        cache_hit=bool(result.get("from_cache")),
        metadata={"requested_modules": result.get("requested_modules", []), "module_status": result.get("module_status", {})},
    )


def register_research_capabilities(
    registry: CapabilityRegistry,
    *,
    engine_factory: Callable[[], Any] = _engine_factory,
    store_factory: Callable[[], Any] = _store_factory,
) -> None:
    def stock(arguments: dict[str, Any], context: ExecutionContext) -> ToolEnvelope:
        ticker = str(arguments.get("ticker") or "").upper()
        focus = str(arguments.get("focus") or "overview")
        modules = _FOCUS_MODULES.get(focus, _FOCUS_MODULES["overview"])
        result = engine_factory().run(ticker, modules=modules)
        return _envelope(result, "research.stock")

    def compare(arguments: dict[str, Any], context: ExecutionContext) -> ToolEnvelope:
        tickers = list(dict.fromkeys(str(value).upper() for value in arguments.get("tickers", [])))[:4]
        dimensions = arguments.get("dimensions") or ["overview"]
        focus = str(dimensions[0]) if dimensions else "overview"
        modules = _FOCUS_MODULES.get(focus, _FOCUS_MODULES["overview"])
        with ThreadPoolExecutor(max_workers=max(1, len(tickers))) as pool:
            rows = list(pool.map(lambda ticker: engine_factory().run(ticker, modules=modules), tickers))
        envelopes = [_envelope(row, "research.stock") for row in rows]
        status = ResultStatus.COMPLETED if all(item.ok for item in envelopes) else ResultStatus.PARTIAL_ERROR
        summaries = [f"{item.subject}: {item.summary}" for item in envelopes if item.summary]
        return ToolEnvelope(
            "research.compare",
            status,
            subject=",".join(tickers),
            summary="\n".join(summaries),
            findings=[finding for item in envelopes for finding in item.findings],
            evidence=[evidence for item in envelopes for evidence in item.evidence],
            limitations=[value for item in envelopes for value in item.limitations],
            metadata={"dimensions": list(dimensions), "research_run_ids": [item.run_id for item in envelopes]},
        )

    def changes(arguments: dict[str, Any], context: ExecutionContext) -> ToolEnvelope:
        ticker = str(arguments.get("ticker") or "").upper()
        result = store_factory().compare_latest(ticker)
        if result is None:
            return ToolEnvelope("research.changes", ResultStatus.PARTIAL_DATA, subject=ticker, limitations=["at least two research snapshots are required"])
        summary = f"{ticker} 的最近两次研究快照已完成比较。"
        evidence = EvidenceItem(
            id=f"research-change-{ticker}-{result.get('new_run_id') or 'latest'}",
            entity=ticker,
            claim=summary,
            source_id="research_store",
            producer_run_id=str(result.get("new_run_id") or ""),
            metadata=result,
        )
        return ToolEnvelope("research.changes", ResultStatus.COMPLETED, subject=ticker, summary=summary, evidence=[evidence], metadata=result)

    registry.register("research.stock", stock)
    registry.register("research.compare", compare)
    registry.register("research.changes", changes)
