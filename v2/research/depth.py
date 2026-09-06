"""Deterministic Phase 3B research-depth helpers.

The functions in this module transform already-fetched evidence.  They never
call chat/LLM code and never manufacture a conclusion when source text is not
available.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from typing import Any


@dataclass(frozen=True)
class SecFinding:
    filing_type: str
    filing_date: str
    accession_number: str
    category: str
    title: str
    summary: str
    evidence_text: str
    source_url: str | None
    confidence: float
    change_type: str = "CURRENT"


_SPACE = re.compile(r"\s+")
_HEADING = re.compile(r"(?m)^(?:#{1,5}\s+|\*\*)([^\n*]{8,180})(?:\*\*)?\s*$")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _text(value: Any, limit: int = 1200) -> str:
    value = _SPACE.sub(" ", str(value or "")).strip()
    return value[:limit]


def _attr(value: Any, name: str, default: Any = "") -> Any:
    try:
        return getattr(value, name, default)
    except Exception:
        return default


def _item(obj: Any, candidates: list[tuple[str | None, str]]) -> str:
    """Read an SEC item across edgartools 10-K/10-Q API variants."""
    for part, name in candidates:
        try:
            if part and hasattr(obj, "get_item_with_part"):
                value = obj.get_item_with_part(part, name, markdown=True)
            elif hasattr(obj, "get_item"):
                value = obj.get_item(name, markdown=True)
            else:
                value = None
            if isinstance(value, str) and value.strip():
                return value
        except Exception:
            continue
    return ""


def _risk_sections(text: str) -> list[dict]:
    if not text:
        return []
    hits = list(_HEADING.finditer(text))
    rows: list[dict] = []
    if not hits:
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if len(p.strip()) >= 80]
        return [{"title": f"Risk factor {index + 1}", "text": _text(p, 900)} for index, p in enumerate(paragraphs[:20])]
    for index, hit in enumerate(hits):
        body = text[hit.end() : hits[index + 1].start() if index + 1 < len(hits) else len(text)]
        body = _text(body, 1200)
        if len(body) >= 60:
            rows.append({"title": _text(hit.group(1), 180), "text": body})
    return rows[:40]


def compare_risk_sections(current: str, previous: str) -> list[dict]:
    """Return conservative NEW/REMOVED/EXPANDED/REDUCED section changes."""
    current_rows, previous_rows = _risk_sections(current), _risk_sections(previous)
    if not current_rows or not previous_rows:
        return []
    matched_previous: set[int] = set()
    changes: list[dict] = []
    for row in current_rows:
        scores = [SequenceMatcher(None, row["title"].lower(), old["title"].lower()).ratio() for old in previous_rows]
        best = max(range(len(scores)), key=scores.__getitem__) if scores else None
        if best is None or scores[best] < .58:
            changes.append({**row, "change_type": "NEW", "previous_length": 0, "current_length": len(row["text"])})
            continue
        matched_previous.add(best)
        old = previous_rows[best]
        ratio = len(row["text"]) / max(1, len(old["text"]))
        content_similarity = SequenceMatcher(None, row["text"][:800], old["text"][:800]).ratio()
        if ratio >= 1.22 and content_similarity < .93:
            changes.append({**row, "change_type": "EXPANDED", "previous_length": len(old["text"]), "current_length": len(row["text"])})
        elif ratio <= .78 and content_similarity < .93:
            changes.append({**row, "change_type": "REDUCED", "previous_length": len(old["text"]), "current_length": len(row["text"])})
    for index, row in enumerate(previous_rows):
        if index not in matched_previous:
            changes.append({**row, "change_type": "REMOVED", "previous_length": len(row["text"]), "current_length": 0})
    return changes[:20]


def _profile_from_business(text: str) -> dict:
    if not text:
        return {"description": None, "business_model": None, "core_products": [], "primary_markets": [], "segments": []}
    sentences = [_text(part, 420) for part in _SENTENCE.split(_text(text, 5000)) if len(part.strip()) > 35]
    description = " ".join(sentences[:2])[:800] or None
    model = next((s for s in sentences if re.search(r"\b(revenue|derive|generate|business model|sales)\b", s, re.I)), None)
    products = [s for s in sentences if re.search(r"\b(product|service|platform|device|software|segment)\b", s, re.I)][:4]
    markets = [s for s in sentences if re.search(r"\b(geographic|international|americas|europe|asia|market)\b", s, re.I)][:3]
    return {"description": description, "business_model": model, "core_products": products, "primary_markets": markets, "segments": []}


def _guidance_from_text(text: str, filing_date: str, source_url: str | None) -> list[dict]:
    if not text:
        return []
    sentences = [_text(part, 520) for part in _SENTENCE.split(_text(text, 12000))]
    rows = []
    for sentence in sentences:
        lower = sentence.lower()
        if not re.search(r"\b(expect|forecast|guidance|outlook|anticipate|project|believe|intend)\w*\b", lower):
            continue
        has_number = bool(re.search(r"(?:\$\s?\d|\d+(?:\.\d+)?\s?%|between\s+\$?\d|range\s+(?:of|from))", lower))
        explicit_guidance = bool(re.search(r"\b(?:guidance|forecast|outlook)\b", lower))
        forward_looking = bool(re.search(r"\b(?:expect|anticipate|project)\w*\b", lower))
        if explicit_guidance and has_number:
            guidance_type = "FORMAL_GUIDANCE"
        elif forward_looking and has_number:
            guidance_type = "QUANTITATIVE_OUTLOOK"
        elif explicit_guidance or forward_looking:
            guidance_type = "QUALITATIVE_OUTLOOK"
        else:
            guidance_type = "MANAGEMENT_COMMENTARY"
        status = "REITERATED"
        if re.search(r"\b(raise|increase|above|higher|improve)\w*\b", lower):
            status = "RAISED"
        elif re.search(r"\b(lower|reduce|below|decline|decrease)\w*\b", lower):
            status = "LOWERED"
        elif re.search(r"\b(withdraw|suspend|no longer provide)\w*\b", lower):
            status = "WITHDRAWN"
        metric = "management_outlook"
        metric_match = re.search(r"\b(revenue|sales|gross margin|operating margin|earnings|eps|capital expenditure|capex|free cash flow)\b", lower)
        if metric_match:
            metric = metric_match.group(1).replace(" ", "_")
        period_match = re.search(r"\b(?:fiscal\s+)?(?:q[1-4]|quarter|year|fy\s?\d{2,4}|20\d{2})\b", lower)
        # Numeric values remain null until a dedicated parser can preserve units
        # and ranges without ambiguity.  The evidence sentence is retained.
        rows.append({"guidance_type": guidance_type, "metric": metric, "period": period_match.group(0) if period_match else None,
                     "value": None, "range": None, "unit": None, "status": status, "direction": status,
                     "evidence_text": sentence, "filing_date": filing_date, "source_url": source_url,
                     "source": "SEC filing", "confidence": .78 if guidance_type == "FORMAL_GUIDANCE" else .7 if guidance_type == "QUANTITATIVE_OUTLOOK" else .58 if guidance_type == "QUALITATIVE_OUTLOOK" else .4})
    return rows[:8]


def parse_sec_filings(ticker: str, filings_by_form: dict[str, list[Any]]) -> dict:
    """Parse recent SEC filing objects into stable, JSON-safe evidence."""
    findings: list[SecFinding] = []
    risk_changes: list[dict] = []
    guidance: list[dict] = []
    profile = {"description": None, "business_model": None, "core_products": [], "primary_markets": [], "segments": []}
    parsed_forms: list[str] = []

    for form in ("10-K", "10-Q"):
        parsed: list[dict] = []
        for filing in filings_by_form.get(form, [])[:2]:
            try:
                obj = filing.obj()
            except Exception:
                continue
            business = _item(obj, [(None, "Item 1"), ("Part I", "Item 1")]) if form == "10-K" else ""
            risk = _item(obj, [(None, "Item 1A"), ("Part II", "Item 1A")])
            mda = _item(obj, [(None, "Item 7"), ("Part I", "Item 2")])
            meta = {
                "date": str(_attr(filing, "filing_date")),
                "accession": str(_attr(filing, "accession_number", _attr(filing, "accession_no"))),
                "url": str(_attr(filing, "homepage_url", _attr(filing, "filing_url"))) or None,
                "business": business, "risk": risk, "mda": mda,
            }
            parsed.append(meta)
            parsed_forms.append(form)
            if business and not profile["description"]:
                profile = _profile_from_business(business)
            for category, title, content in (("BUSINESS", "Business overview", business), ("RISK", "Risk factors", risk), ("MD&A", "Management discussion", mda)):
                if content:
                    findings.append(SecFinding(form, meta["date"], meta["accession"], category, title, _text(content, 420), _text(content, 900), meta["url"], .82))
            guidance.extend(_guidance_from_text(mda, meta["date"], meta["url"]))
        if len(parsed) >= 2:
            for change in compare_risk_sections(parsed[0]["risk"], parsed[1]["risk"]):
                risk_changes.append({**change, "filing_type": form, "filing_date": parsed[0]["date"], "source_url": parsed[0]["url"], "confidence": .72})

    # 8-K classification reuses the existing audited item table.
    try:
        from v2.sec.eight_k_parser import parse_eight_k_filing
        from v2.sec.models import SecFiling
        for filing in filings_by_form.get("8-K", [])[:12]:
            meta = SecFiling(ticker=ticker, cik=str(_attr(filing, "cik")), form=str(_attr(filing, "form", "8-K")), filing_date=str(_attr(filing, "filing_date")), accession_number=str(_attr(filing, "accession_number", _attr(filing, "accession_no"))))
            event = parse_eight_k_filing(filing, meta)
            if not event:
                continue
            parsed_forms.append("8-K")
            url = str(_attr(filing, "homepage_url", _attr(filing, "filing_url"))) or None
            for item in event.items:
                findings.append(SecFinding("8-K", meta.filing_date, meta.accession_number, "MATERIAL_EVENT", f"Item {item.code}: {item.description}", item.description, item.description, url, .9, "NEW"))
    except Exception:
        pass

    return {
        "findings": [asdict(item) for item in findings],
        "risk_factor_changes": risk_changes,
        "guidance": guidance,
        "company_profile": profile,
        "parsed_forms": sorted(set(parsed_forms)),
    }


def classify_catalysts(items: list[dict]) -> list[dict]:
    """De-duplicate and distinguish ordinary news, events and catalysts."""
    output: list[dict] = []
    seen: set[str] = set()
    catalyst_re = re.compile(r"\b(approval|approved|acquisition|merger|contract|guidance|launch|buyback|dividend|partnership|settlement|investigation|recall|bankruptcy|restructur)\w*\b", re.I)
    for item in items:
        title = _text(item.get("title"), 300)
        key = re.sub(r"[^a-z0-9]", "", title.lower())[:120] + str(item.get("event_date") or "")[:10]
        if not title or key in seen:
            continue
        seen.add(key)
        source_kind = str(item.get("source_kind") or "NEWS").upper()
        if source_kind == "EVENT":
            kind, reason = "EVENT", "有明确日程的已知事件"
        elif catalyst_re.search(title):
            kind, reason = "CATALYST", "标题包含可能改变基本面或预期的可验证事件"
        else:
            kind, reason = "NEWS", "普通公司新闻，尚不足以定义为催化剂"
        output.append({**item, "item_type": kind, "classification_reason": reason})
    return output


EXPECTATIONS_CAPABILITY = {
    "current_consensus": {"status": "AVAILABLE_WHEN_PROVIDER_RETURNS", "fabricated": False},
    "earnings_surprise_history": {"status": "AVAILABLE", "fabricated": False},
    "revision_30d": {"status": "UNAVAILABLE", "reason": "No historical consensus snapshot provider", "fabricated": False},
    "revision_60d": {"status": "UNAVAILABLE", "reason": "No historical consensus snapshot provider", "fabricated": False},
    "revision_90d": {"status": "UNAVAILABLE", "reason": "No historical consensus snapshot provider", "fabricated": False},
}


def sanitize_error(error: Any) -> str:
    """Remove credentials and query-string secrets before persistence/API output."""
    message = str(error)
    message = re.sub(r"(?i)(api[_-]?key|token|authorization|x-api-key)(\s*[:=]\s*)([^\s,;&]+)", r"\1\2[REDACTED]", message)
    message = re.sub(r"(?i)([?&](?:api[_-]?key|token)=)[^&\s]+", r"\1[REDACTED]", message)
    return message[:800]
