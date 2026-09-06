"""Deterministic cross-module research intelligence (Phase 3C).

This layer consumes normalized Phase 3A/3B module results.  It does not fetch
data and does not call an LLM.  Every conclusion retains a path back to a
module evidence record and its source identifiers.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _num(value: Any) -> float | None:
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _pct(value: float | None) -> str:
    return "N/A" if value is None else f"{value * 100:+.1f}%"


def _id(*parts: Any) -> str:
    raw = "|".join(str(part or "") for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class ResearchFinding:
    id: str
    ticker: str
    module: str
    category: str
    title: str
    claim: str
    direction: str
    importance: str
    confidence: float
    evidence_ids: list[str]
    source_ids: list[str]
    metrics: dict[str, Any]
    generated_at: str


PROFILES = {
    "TECHNOLOGY": {
        "description": "科技企业强调增长、毛利率、ROIC、自由现金流、研发投入、SBC 与估值匹配。",
        "weights": {"growth": .25, "profitability": .25, "capital_efficiency": .20, "financial_health": .10, "valuation": .20},
        "preferred_metrics": ["revenue_growth", "gross_margin", "roic", "free_cash_flow", "research_and_development", "stock_based_compensation"],
    },
    "FINANCIALS": {
        "description": "金融机构使用 ROE、盈利增长、资本充足、净息差、信贷质量和存款趋势；不使用普通企业杠杆逻辑。",
        "weights": {"growth": .20, "roe": .30, "earnings": .20, "valuation": .20, "capital_quality": .10},
        "preferred_metrics": ["roe", "net_interest_margin", "cet1", "loan_growth", "credit_quality", "deposit_growth"],
    },
    "ENERGY": {
        "description": "能源企业结合商品周期、产量、自由现金流、资本开支、资产负债表和盈亏平衡成本。",
        "weights": {"cash_flow": .25, "profitability": .20, "balance_sheet": .15, "valuation": .15, "commodity_cycle": .25},
        "preferred_metrics": ["free_cash_flow", "capital_expenditure", "roic", "wti_crude", "production", "breakeven"],
    },
    "GENERAL": {
        "description": "通用企业综合增长、盈利质量、资本效率、财务健康和估值。",
        "weights": {"growth": .25, "profitability": .25, "capital_efficiency": .20, "financial_health": .15, "valuation": .15},
        "preferred_metrics": ["revenue_growth", "gross_margin", "operating_margin", "roic", "free_cash_flow"],
    },
}


_PROFILE_OVERRIDES = {"JPM": "FINANCIALS", "XOM": "ENERGY", "AAPL": "TECHNOLOGY", "NVDA": "TECHNOLOGY"}


def industry_context(company: dict) -> dict:
    sector = str(company.get("sector") or "").lower()
    industry = str(company.get("industry") or "").lower()
    ticker = str(company.get("ticker") or "").upper()
    if ticker in _PROFILE_OVERRIDES:
        profile = _PROFILE_OVERRIDES[ticker]
    elif any(word in sector + " " + industry for word in ("financial", "bank", "insurance", "credit")):
        profile = "FINANCIALS"
    elif any(word in sector + " " + industry for word in ("energy", "oil", "gas", "petroleum")):
        profile = "ENERGY"
    elif any(word in sector + " " + industry for word in ("technology", "semiconductor", "software", "hardware")):
        profile = "TECHNOLOGY"
    else:
        profile = "GENERAL"
    return {"profile": profile, "sector": company.get("sector"), "industry": company.get("industry"), **PROFILES[profile]}


def _module_confidence(module: dict) -> float:
    confidence = _num(module.get("confidence")) or 0.0
    completeness = _num(module.get("completeness"))
    if completeness is None:
        completeness = 1.0 if module.get("status") == "COMPLETED" else .55 if module.get("status") in {"PARTIAL", "PARTIAL_DATA"} else .15
    if completeness > 1:
        completeness /= 100
    return max(0.0, min(1.0, min(confidence, completeness)))


def _source_set(module: dict) -> list[str]:
    return list(dict.fromkeys(module.get("data_sources_used") or module.get("sources") or []))


def _make_finding(ticker: str, module_name: str, module: dict, category: str, title: str, claim: str,
                  direction: str, importance: str, metrics: dict | None = None, source_ids: list[str] | None = None,
                  confidence: float | None = None) -> tuple[dict, dict]:
    sources = list(dict.fromkeys(source_ids if source_ids is not None else _source_set(module)))
    cap = _module_confidence(module)
    confidence = min(cap, confidence if confidence is not None else cap)
    if not sources:
        confidence = min(confidence, .30)
        if importance in {"HIGH", "CRITICAL"}:
            importance = "MEDIUM"
    evidence_id = f"evidence-{_id(ticker, module_name, category, title, sources)}"
    finding_id = f"finding-{_id(ticker, module_name, category, title, claim)}"
    finding = ResearchFinding(finding_id, ticker, module_name, category, title, claim, direction, importance,
                              round(confidence, 3), [evidence_id], sources, metrics or {}, _now())
    evidence = {"id": evidence_id, "ticker": ticker, "module": module_name, "claim": claim,
                "metrics": metrics or {}, "source_ids": sources, "verified": bool(sources), "generated_at": _now()}
    return asdict(finding), evidence


def build_findings(ticker: str, modules: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    findings: list[dict] = []
    evidence: list[dict] = []

    def add(module_name: str, category: str, title: str, claim: str, direction: str, importance: str,
            metrics: dict | None = None, sources: list[str] | None = None, confidence: float | None = None) -> None:
        finding, item = _make_finding(ticker, module_name, modules.get(module_name, {}), category, title, claim,
                                      direction, importance, metrics, sources, confidence)
        findings.append(finding)
        evidence.append(item)

    f = modules.get("fundamental", {})
    fm = f.get("metrics", {})
    growth = _num(fm.get("revenue_growth"))
    if growth is not None:
        add("fundamental", "growth", "Revenue growth", f"Revenue growth is {_pct(growth)} on the latest available basis.",
            "POSITIVE" if growth >= .08 else "NEGATIVE" if growth < 0 else "NEUTRAL", "HIGH", {"revenue_growth": growth})
    margin = _num(fm.get("gross_margin"))
    roic = _num(fm.get("roic"))
    if margin is not None or roic is not None:
        direction = "POSITIVE" if (roic or 0) >= .12 or (margin or 0) >= .45 else "NEGATIVE" if (roic is not None and roic < .04) else "NEUTRAL"
        add("fundamental", "profitability", "Profitability and capital returns",
            f"Gross margin is {_pct(margin)} and ROIC is {_pct(roic)}.", direction, "HIGH", {"gross_margin": margin, "roic": roic})
    allocation = f.get("details", {}).get("capital_allocation", {})
    if allocation:
        assessment = allocation.get("assessment", "INSUFFICIENT_EVIDENCE")
        add("fundamental", "capital_allocation", "Capital allocation", f"Capital allocation assessment is {assessment}.",
            "POSITIVE" if assessment == "SHAREHOLDER_RETURN" else "NEGATIVE" if assessment == "DILUTIVE" else "NEUTRAL",
            "MEDIUM", allocation)

    v = modules.get("valuation", {})
    pe = _num(v.get("metrics", {}).get("pe_ttm"))
    if pe is not None:
        add("valuation", "valuation", "Absolute valuation", f"TTM P/E is {pe:.1f}x.",
            "NEGATIVE" if pe >= 40 else "POSITIVE" if pe <= 18 else "NEUTRAL", "HIGH", {"pe_ttm": pe})

    e = modules.get("earnings", {})
    surprise = _num(e.get("metrics", {}).get("latest_eps_surprise"))
    if surprise is not None:
        add("earnings", "earnings", "Latest earnings surprise", f"Latest EPS surprise is {_pct(surprise)}.",
            "POSITIVE" if surprise > 0 else "NEGATIVE", "MEDIUM", {"eps_surprise": surprise})

    exp = modules.get("expectations", {})
    revision = exp.get("metrics", {}).get("revision_trend")
    if revision:
        text = str(revision).upper()
        add("expectations", "expectations", "Estimate revisions", f"Estimate revision trend is {revision}.",
            "POSITIVE" if text in {"UP", "POSITIVE", "RISING"} else "NEGATIVE" if text in {"DOWN", "NEGATIVE", "FALLING"} else "NEUTRAL",
            "HIGH", {"revision_trend": revision})
    else:
        add("expectations", "expectations", "Expectations history unavailable",
            "Historical estimate revisions are unavailable; no revision trend is inferred.", "NEUTRAL", "LOW", {}, [], .2)

    sec = modules.get("sec", {})
    for row in sec.get("details", {}).get("guidance", [])[:3]:
        status = str(row.get("status") or row.get("direction") or "UNCHANGED").upper()
        add("sec", "guidance", f"{row.get('guidance_type', 'MANAGEMENT_COMMENTARY')}: {status}",
            str(row.get("evidence_text") or "Management outlook was identified in a filing."),
            "NEGATIVE" if status in {"LOWERED", "WITHDRAWN"} else "POSITIVE" if status == "RAISED" else "NEUTRAL",
            "HIGH" if row.get("guidance_type") in {"FORMAL_GUIDANCE", "QUANTITATIVE_OUTLOOK"} else "MEDIUM",
            {key: row.get(key) for key in ("guidance_type", "metric", "period", "value", "range", "status")}, ["sec_filings"], row.get("confidence", .6))
    changes = sec.get("details", {}).get("risk_factor_changes", [])
    if changes:
        add("sec", "disclosure_change", "SEC risk-factor changes", f"{len(changes)} risk-factor changes were detected against the prior filing.",
            "NEGATIVE" if any(row.get("change_type") in {"NEW", "EXPANDED"} for row in changes) else "NEUTRAL", "HIGH",
            {"change_count": len(changes)}, ["sec_filings"], .72)

    inst = modules.get("institutional", {})
    buys = _num(inst.get("metrics", {}).get("insider_buy_value_90d"))
    sells = _num(inst.get("metrics", {}).get("insider_sell_value_90d"))
    if buys is not None or sells is not None:
        net = (buys or 0) - (sells or 0)
        add("institutional", "positioning", "Reported insider activity",
            f"Reported 90-day insider net transaction value is {net:,.0f}; this is not real-time institutional flow.",
            "POSITIVE" if net > 0 else "NEGATIVE" if net < 0 else "NEUTRAL", "MEDIUM", {"net_insider_value": net})

    tech = modules.get("technical", {})
    trend = tech.get("metrics", {}).get("trend_state") or tech.get("summary")
    if tech.get("status") not in {"FAILED", "SKIPPED"}:
        text = str(trend).lower()
        add("technical", "technical", "Technical regime", f"Current daily technical regime is {trend}.",
            "POSITIVE" if "bull" in text or "up" in text else "NEGATIVE" if "bear" in text or "down" in text else "NEUTRAL",
            "MEDIUM", {"trend": trend, "timeframe": tech.get("metrics", {}).get("timeframe", "1d")})

    for row in modules.get("catalyst", {}).get("details", {}).get("timeline", [])[:5]:
        direction = str(row.get("direction") or "neutral").upper()
        add("catalyst", "catalyst", str(row.get("title") or "Known event"), str(row.get("description") or "Known dated event."),
            direction if direction in {"POSITIVE", "NEGATIVE", "NEUTRAL"} else "NEUTRAL", "HIGH" if row.get("impact") in {"High", "Very High"} else "MEDIUM",
            {"event_date": row.get("event_date"), "item_type": row.get("item_type")}, [row.get("source_id")] if row.get("source_id") else [], row.get("confidence", .6))

    macro = modules.get("macro", {})
    vix = _num(macro.get("metrics", {}).get("vix"))
    wti = _num(macro.get("metrics", {}).get("wti_crude"))
    if vix is not None or wti is not None:
        add("macro", "macro", "Macro market backdrop", f"VIX is {vix if vix is not None else 'N/A'} and WTI is {wti if wti is not None else 'N/A'}.",
            "NEGATIVE" if vix is not None and vix >= 30 else "NEUTRAL", "MEDIUM", {"vix": vix, "wti_crude": wti})

    chain = modules.get("supply_chain", {})
    rels = chain.get("details", {}).get("relationships", [])
    verified = [row for row in rels if row.get("verified")]
    if rels:
        names = ", ".join(str(row.get("target_company")) for row in verified[:4]) or "none independently verified"
        add("supply_chain", "supply_chain", "Validated business relationships",
            f"{len(verified)} of {len(rels)} identified relationships are externally verified ({names}).", "NEUTRAL", "HIGH" if verified else "MEDIUM",
            {"relationship_count": len(rels), "verified_count": len(verified)}, ["supply_chain"] if verified else [], .75 if verified else .3)

    risk = modules.get("risk", {})
    for row in risk.get("details", {}).get("radar", []):
        if row.get("level") not in {"MEDIUM", "HIGH"}:
            continue
        add("risk", "risk", f"{row.get('name')} risk", str(row.get("reason") or "Risk evidence identified."), "NEGATIVE",
            "CRITICAL" if row.get("level") == "HIGH" else "HIGH", {"level": row.get("level"), "trend": row.get("trend")},
            list(row.get("evidence") or []), row.get("confidence", .5))
    return findings, evidence


def apply_peer_relevance(ticker: str, company: dict, modules: dict[str, dict]) -> list[dict]:
    rows = modules.get("valuation", {}).get("details", {}).get("peer_comparison", [])
    target_sector = str(company.get("sector") or "").lower()
    target_industry = str(company.get("industry") or "").lower()
    target_tokens = set((target_sector + " " + target_industry).replace("-", " ").split())
    relationships = modules.get("supply_chain", {}).get("details", {}).get("relationships", [])
    competitors = {str(r.get("target_company") or "").upper() for r in relationships if str(r.get("relationship_type") or "").lower() in {"competitor", "smaller_peer", "substitute"}}
    output = []
    for row in rows:
        peer_sector = str(row.get("sector") or "").lower()
        peer_industry = str(row.get("industry") or "").lower()
        peer_tokens = set((peer_sector + " " + peer_industry).replace("-", " ").split())
        score = 0
        factors = {}
        factors["sector"] = 35 if target_sector and target_sector == peer_sector else 0
        factors["industry"] = 30 if target_industry and target_industry == peer_industry else 15 if target_tokens & peer_tokens else 0
        factors["business_model"] = min(15, 5 * len(target_tokens & peer_tokens))
        factors["competitive_relationship"] = 15 if str(row.get("ticker") or "").upper() in competitors else 0
        factors["financial_comparability"] = 5 if row.get("revenue_growth") is not None and row.get("operating_margin") is not None else 0
        score = min(100, sum(factors.values()))
        label = "CORE_PEER" if score >= 70 else "SECONDARY_PEER" if score >= 45 else "LOW_RELEVANCE"
        output.append({**row, "relevance_score": score, "relevance_label": label, "comparison_weight": round(score / 100, 2), "relevance_factors": factors})
    modules.get("valuation", {}).get("details", {})["peer_comparison"] = output
    return output


def sector_aware_scores(context: dict, modules: dict[str, dict]) -> dict:
    f = modules.get("fundamental", {})
    fm = f.get("metrics", {})
    profile = context["profile"]
    unavailable = []
    components = {}
    if profile == "FINANCIALS":
        components = {"growth": fm.get("growth_score"), "roe": None if _num(fm.get("roe")) is None else max(0, min(100, _num(fm.get("roe")) * 400)), "earnings": modules.get("earnings", {}).get("score"), "valuation": modules.get("valuation", {}).get("score"), "capital_quality": None}
        unavailable = [name for name in ("net_interest_margin", "cet1", "loan_growth", "credit_quality", "deposit_growth") if fm.get(name) is None]
    elif profile == "ENERGY":
        components = {"cash_flow": fm.get("growth_score"), "profitability": fm.get("profitability_score"), "balance_sheet": fm.get("financial_health_score"), "valuation": modules.get("valuation", {}).get("score"), "commodity_cycle": 50 if modules.get("macro", {}).get("metrics", {}).get("wti_crude") is not None else None}
        unavailable = [name for name in ("production", "breakeven") if fm.get(name) is None]
    else:
        components = {"growth": fm.get("growth_score"), "profitability": fm.get("profitability_score"), "capital_efficiency": None if _num(fm.get("roic")) is None else max(0, min(100, _num(fm.get("roic")) * 350)), "financial_health": fm.get("financial_health_score"), "valuation": modules.get("valuation", {}).get("score")}
        if profile == "TECHNOLOGY":
            allocation = f.get("details", {}).get("capital_allocation", {})
            unavailable = [name for name in ("research_and_development", "stock_based_compensation") if allocation.get(name) is None]
    values = [(float(value), context["weights"].get(key, 0)) for key, value in components.items() if _num(value) is not None]
    overall = round(sum(value * weight for value, weight in values) / sum(weight for _, weight in values)) if values and sum(weight for _, weight in values) else None
    return {"profile": profile, "overall": overall, "components": components, "weights": context["weights"], "unavailable_metrics": unavailable,
            "excluded_metrics": ["debt_to_equity", "current_ratio", "interest_coverage"] if profile == "FINANCIALS" else []}


def research_confidence(modules: dict[str, dict], findings: list[dict], evidence: list[dict]) -> dict:
    core_weights = {"fundamental": .20, "valuation": .15, "sec": .15, "earnings": .10, "expectations": .08, "institutional": .07, "technical": .05, "catalyst": .06, "macro": .04, "supply_chain": .05, "risk": .05}
    module_rows = {}
    total = weight_total = 0.0
    for name, weight in core_weights.items():
        module = modules.get(name, {})
        completeness = _num(module.get("completeness"))
        if completeness is None:
            completeness = 1 if module.get("status") == "COMPLETED" else .55 if module.get("status") in {"PARTIAL", "PARTIAL_DATA"} else .1
        completeness = completeness / 100 if completeness > 1 else completeness
        confidence = _num(module.get("confidence")) or 0
        verified = _num(module.get("verified_source_count"))
        count = _num(module.get("source_count"))
        verification = (verified / count) if count else (1 if _source_set(module) else 0)
        score = max(0, min(100, round(100 * (.5 * completeness + .35 * confidence + .15 * verification))))
        # A well-known source cannot compensate for a mostly missing module.
        # Completeness therefore acts as a deterministic ceiling.
        if completeness < .30:
            score = min(score, 34)
        elif completeness < .50:
            score = min(score, 49)
        module_rows[name] = {"score": score, "band": "HIGH" if score >= 75 else "MEDIUM" if score >= 45 else "LOW", "completeness": round(completeness, 3), "missing_fields": module.get("missing_fields", [])}
        total += score * weight
        weight_total += weight
    verified_ratio = sum(bool(row.get("verified")) for row in evidence) / max(1, len(evidence))
    score = round((total / max(weight_total, .01)) * (.85 + .15 * verified_ratio))
    weak_core = sum(module_rows[name]["score"] < 35 for name in ("fundamental", "valuation", "sec"))
    if weak_core >= 2:
        score = min(score, 49)
    elif weak_core == 1:
        score = min(score, 69)
    return {"score": score, "band": "HIGH" if score >= 75 else "MEDIUM" if score >= 45 else "LOW", "modules": module_rows,
            "verified_evidence_ratio": round(verified_ratio, 3), "method": "deterministic completeness/confidence/source-verification weighting"}


def build_drivers(findings: list[dict]) -> dict:
    rank = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}
    result = {"positive": [], "negative": []}
    for direction, key in (("POSITIVE", "positive"), ("NEGATIVE", "negative")):
        selected = sorted((f for f in findings if f["direction"] == direction), key=lambda f: (rank[f["importance"]], f["confidence"]), reverse=True)
        seen = set()
        for finding in selected:
            identity = (finding["category"], finding["title"])
            if identity in seen:
                continue
            seen.add(identity)
            result[key].append({"id": f"driver-{_id(finding['ticker'], direction, *identity)}", "driver_type": finding["category"].upper(),
                                "title": finding["title"], "direction": direction, "importance": finding["importance"],
                                "confidence": finding["confidence"], "findings": [finding["id"]], "evidence": finding["evidence_ids"]})
            if len(result[key]) == 5:
                break
    return result


def detect_conflicts(findings: list[dict]) -> list[dict]:
    by_category = {category: [f for f in findings if f["category"] == category] for category in {f["category"] for f in findings}}
    rules = [("growth", "expectations", "FUNDAMENTAL_VS_EXPECTATIONS"), ("growth", "technical", "FUNDAMENTAL_VS_TECHNICAL"), ("profitability", "valuation", "QUALITY_VS_VALUATION"), ("growth", "positioning", "FUNDAMENTAL_VS_POSITIONING")]
    conflicts = []
    for left, right, kind in rules:
        for a in by_category.get(left, []):
            for b in by_category.get(right, []):
                if {a["direction"], b["direction"]} == {"POSITIVE", "NEGATIVE"}:
                    conflicts.append({"id": f"conflict-{_id(kind, a['id'], b['id'])}", "type": kind, "description": f"{a['title']} conflicts with {b['title']}.",
                                      "severity": "HIGH" if min(a["confidence"], b["confidence"]) >= .75 else "MEDIUM", "finding_ids": [a["id"], b["id"]], "evidence_ids": a["evidence_ids"] + b["evidence_ids"]})
    return conflicts


def quality_gate(modules: dict[str, dict], confidence: dict) -> dict:
    essential = ("fundamental", "valuation", "sec")
    coverage = round(sum(confidence["modules"][name]["score"] for name in confidence["modules"]) / len(confidence["modules"]))
    warnings = []
    severe = []
    for name in essential:
        module = modules.get(name, {})
        has_payload = bool(module.get("metrics") or module.get("details", {}).get("sec_findings") or module.get("details", {}).get("filings"))
        if module.get("status") in {"FAILED", "SKIPPED"} or confidence["modules"][name]["score"] < 35 or not has_payload:
            warnings.append(f"Core module {name} has insufficient coverage.")
            severe.append(name)
    if modules.get("expectations", {}).get("metrics", {}).get("revision_trend") is None:
        warnings.append("Historical expectations are unavailable; no revision trend was inferred.")
    limited = coverage < 60 or len(severe) >= 2
    return {"status": "LIMITED_RESEARCH_THESIS" if limited else "PASSED", "coverage": coverage, "warnings": warnings,
            "checks": {name: confidence["modules"][name]["score"] for name in essential}, "source_verification": confidence["verified_evidence_ratio"]}


def build_scenarios(ticker: str, drivers: dict) -> dict:
    positive, negative = drivers["positive"], drivers["negative"]
    def assumption(driver: dict, condition: str) -> dict:
        return {"driver_id": driver["id"], "driver": driver["title"], "condition": condition, "finding_ids": driver["findings"], "evidence_ids": driver["evidence"]}
    bull = [assumption(d, "Evidence strengthens beyond the current observed trend.") for d in positive[:3]]
    base = [assumption(d, "The latest evidence remains broadly stable without a material break.") for d in (positive[:2] + negative[:1])]
    bear = [assumption(d, "The identified risk persists or becomes more consequential.") for d in negative[:3]]
    def narrative(label: str, rows: list[dict]) -> str:
        subjects = "; ".join(row["driver"] for row in rows) or "available evidence remains insufficient"
        return f"{ticker} {label} scenario: {subjects}."
    return {"BULL": {"scenario": "BULL", "assumptions": bull, "narrative": narrative("bull", bull)},
            "BASE": {"scenario": "BASE", "assumptions": base, "narrative": narrative("base", base)},
            "BEAR": {"scenario": "BEAR", "assumptions": bear, "narrative": narrative("bear", bear)}}


def build_invalidations(drivers: dict) -> list[dict]:
    monitors = {"GROWTH": "reported revenue growth and management guidance", "PROFITABILITY": "gross margin and ROIC",
                "CAPITAL_ALLOCATION": "share count, buybacks, SBC and cash returns", "VALUATION": "peer-relative multiples and realized growth",
                "SUPPLY_CHAIN": "verified supplier/customer disclosures", "TECHNICAL": "daily trend and key levels", "EARNINGS": "earnings surprise and guidance"}
    output = []
    for driver in drivers["positive"]:
        monitor = monitors.get(driver["driver_type"], "the observable evidence supporting this driver")
        output.append({"driver_id": driver["id"], "driver": driver["title"], "monitor": monitor,
                       "condition": "Sustained material deterioration relative to the currently documented evidence and expectations.",
                       "finding_ids": driver["findings"], "evidence_ids": driver["evidence"]})
    return output[:5]


def build_intelligence(ticker: str, modules: dict[str, dict], company: dict) -> dict:
    company = {"ticker": ticker, **company}
    context = industry_context(company)
    peers = apply_peer_relevance(ticker, company, modules)
    findings, evidence = build_findings(ticker, modules)
    drivers = build_drivers(findings)
    conflicts = detect_conflicts(findings)
    confidence = research_confidence(modules, findings, evidence)
    gate = quality_gate(modules, confidence)
    scenarios = build_scenarios(ticker, drivers)
    invalidations = build_invalidations(drivers)
    scoring = sector_aware_scores(context, modules)
    name = company.get("name") or ticker
    positives = ", ".join(d["title"] for d in drivers["positive"][:3]) or "no high-quality positive driver"
    negatives = ", ".join(d["title"] for d in drivers["negative"][:3]) or "no high-quality negative driver"
    prefix = "LIMITED RESEARCH THESIS — " if gate["status"] != "PASSED" else ""
    core = f"{prefix}{name}: the evidence currently balances {positives} against {negatives}."
    why_now_items = [f["title"] for f in findings if f["category"] in {"catalyst", "guidance", "disclosure_change", "technical"}][:3]
    why_now = f"Current decision context is shaped by {', '.join(why_now_items)}." if why_now_items else "No sufficiently supported near-term timing signal is available."
    catalysts = [f for f in findings if f["category"] == "catalyst"][:5]
    risks = [f for f in findings if f["direction"] == "NEGATIVE" and f["category"] in {"risk", "supply_chain", "guidance", "valuation"}][:5]
    return {"schema_version": "research-intelligence-v1", "research_findings": findings, "evidence_index": evidence,
            "key_drivers": drivers, "peer_relevance": peers, "industry_context": context, "scoring_profile": scoring,
            "research_confidence": confidence, "conflicts": conflicts, "quality_gate": gate, "scenarios": scenarios,
            "core_thesis": core, "why_now": why_now, "thesis_invalidation": invalidations,
            "key_catalysts": catalysts, "key_risks_v2": risks,
            "investment_thesis_v2": {"core_thesis": core, "why_now": why_now, "positive_drivers": drivers["positive"],
                                      "negative_drivers": drivers["negative"], "bull_case": scenarios["BULL"],
                                      "base_case": scenarios["BASE"], "bear_case": scenarios["BEAR"],
                                      "key_catalysts": catalysts, "key_risks": risks, "thesis_invalidation": invalidations,
                                      "conflicting_evidence": conflicts, "decision_support_only": True}}
