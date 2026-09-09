"""The filing reader: a bounded sub-agent that reads SEC filings around a date.

It lives behind the capability interface like any pure function: parameters
in, an evidence envelope out.  Inside, a small loop lets the model decide
which filing section to read next, at most a few rounds, and every event it
reports must quote text it actually read.  It never talks to the user and
never decides whether it should run; the planner does that.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Protocol

from v2.agent_v2.execution import CapabilityRegistry, ExecutionContext
from v2.agent_v2.models import EvidenceItem, ResultStatus, ToolEnvelope


@dataclass
class FilingRef:
    ticker: str
    form: str
    filing_date: str
    accession: str
    url: str = ""
    raw: Any = field(default=None, repr=False)


@dataclass
class Section:
    id: str
    title: str
    chars: int


class FilingSource(Protocol):
    def list_filings(self, ticker: str, since: str, until: str) -> list[FilingRef]: ...

    def outline(self, ref: FilingRef) -> list[Section]: ...

    def read(self, ref: FilingRef, section_id: str) -> str: ...


_ITEM_HEADER = re.compile(r"(?im)^\s*item\s+(\d{1,2}\.\d{2})\b[^\n]{0,90}")
_HEADING = re.compile(r"(?m)^\s*(?:exhibit\s+\d+(?:\.\d+)?|[A-Z][A-Z &,'()/-]{6,70})\s*$")
_WS = re.compile(r"\s+")


def sections_of(text: str, form: str, *, chunk: int = 3500) -> list[tuple[str, str, str]]:
    """Split a filing's text into (id, title, body) sections.

    8-K bodies are cut at their ``Item X.YY`` headers; other forms at
    upper-case heading lines; anything else into fixed-size parts so the
    reader can still page through it.
    """

    text = text or ""
    if not text.strip():
        return []
    matches = list(_ITEM_HEADER.finditer(text)) if form.upper().startswith("8-K") else []
    if len(matches) < 2:
        matches = [match for match in _HEADING.finditer(text) if len(match.group(0).strip()) >= 8]
    if len(matches) >= 2:
        parts: list[tuple[str, str, str]] = []
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            title = _WS.sub(" ", match.group(0)).strip()[:90]
            body = text[match.start():end].strip()
            if body:
                parts.append((f"s{index + 1}", title, body))
        if parts:
            return parts
    return [(f"part-{index + 1}", f"第 {index + 1} 段", text[start:start + chunk]) for index, start in enumerate(range(0, len(text), chunk))][:12]


class EdgarFilingSource:
    """Filings and their text through the existing EDGAR client and edgartools objects."""

    def __init__(self, fetch: Callable[[str, str, str, str], list[Any]] | None = None) -> None:
        self._fetch = fetch
        self._texts: dict[str, str] = {}
        self._sections: dict[str, list[tuple[str, str, str]]] = {}

    def _rows(self, ticker: str, form: str, since: str, until: str) -> list[Any]:
        if self._fetch is not None:
            return list(self._fetch(ticker, form, since, until) or [])
        from v2.sec import client as sec_client

        return list(sec_client.get_recent_filings(ticker, form, since, until) or [])

    def list_filings(self, ticker: str, since: str, until: str) -> list[FilingRef]:
        refs: list[FilingRef] = []
        for form in ("8-K", "6-K"):
            for raw in self._rows(ticker, form, since, until):
                accession = str(getattr(raw, "accession_number", None) or getattr(raw, "accession_no", None) or "").strip()
                filing_date = str(getattr(raw, "filing_date", "") or "")[:10]
                actual_form = str(getattr(raw, "form", form) or form)
                url = str(getattr(raw, "homepage_url", None) or getattr(raw, "url", None) or "")
                if not url:
                    cik = str(getattr(raw, "cik", "") or "").lstrip("0")
                    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/" if cik and accession else ""
                if accession:
                    refs.append(FilingRef(ticker, actual_form, filing_date, accession, url, raw))
            if refs:
                break  # a domestic filer has 8-Ks; only a foreign issuer needs the 6-K pass
        refs.sort(key=lambda ref: ref.filing_date, reverse=True)
        return refs

    def _text(self, ref: FilingRef) -> str:
        if ref.accession in self._texts:
            return self._texts[ref.accession]
        text = ""
        for attr in ("text", "markdown"):
            method = getattr(ref.raw, attr, None)
            if callable(method):
                try:
                    text = str(method() or "")
                except Exception:  # noqa: BLE001 — a document that fails to load reads as empty
                    text = ""
                if text.strip():
                    break
        self._texts[ref.accession] = text
        return text

    def outline(self, ref: FilingRef) -> list[Section]:
        if ref.accession not in self._sections:
            self._sections[ref.accession] = sections_of(self._text(ref), ref.form)
        return [Section(section_id, title, len(body)) for section_id, title, body in self._sections[ref.accession]]

    def read(self, ref: FilingRef, section_id: str) -> str:
        self.outline(ref)
        for candidate, _, body in self._sections.get(ref.accession, []):
            if candidate == section_id:
                return body
        return ""


_SYSTEM = """你是申报阅读者，只输出 JSON，不回答用户问题。
任务：在给定的 SEC 申报里找出与指定日期附近股价下跌可能相关的、有明确日期的事件（财报数字、指引、高管变动、诉讼、发行、重大合同、监管事项等）。
每一轮只能做一件事：
- 读一节：{"action":"read","filing":<申报序号>,"section":"<章节 id>"}
- 结束：{"action":"finish","events":[{"date":"YYYY-MM-DD","summary":"一句中文概括","quote":"从已读章节里原样复制的一段原文（不超过 300 字符）","filing":<申报序号>,"section":"<章节 id>"}],"note":"一句话说明还缺什么或为什么结束"}
规则：quote 必须逐字来自你已经读过的章节文本，不能改写、不能翻译；没有相关事件就返回空的 events 并在 note 里说明；不要编造日期。"""


class FilingReader:
    """Read the filings around a date and report dated, quoted events."""

    def __init__(self, llm: Any, source: FilingSource, *, max_rounds: int = 4, max_seconds: float = 60.0, max_filings: int = 2, max_chars: int = 6000) -> None:
        self.llm = llm
        self.source = source
        self.max_rounds = max(1, max_rounds)
        self.max_seconds = max(5.0, max_seconds)
        self.max_filings = max(1, max_filings)
        self.max_chars = max(1000, max_chars)

    def run(self, ticker: str, context: ExecutionContext, *, since: str = "", until: str = "", around: str = "", max_filings: int | None = None, today: date | None = None) -> ToolEnvelope:
        current = today or date.today()
        if around:
            anchor = date.fromisoformat(around[:10])
            since = since or (anchor - timedelta(days=14)).isoformat()
            until = until or min(current, anchor + timedelta(days=3)).isoformat()
        since = since or (current - timedelta(days=90)).isoformat()
        until = until or current.isoformat()
        started = time.monotonic()
        refs = self.source.list_filings(ticker, since, until)
        limit = max(1, min(int(max_filings or self.max_filings), 3))
        if around:
            refs.sort(key=lambda ref: abs((date.fromisoformat(ref.filing_date) - date.fromisoformat(around[:10])).days) if ref.filing_date else 999)
        chosen = refs[:limit]
        window = f"{since} 至 {until}"
        if not chosen:
            return self._envelope(ticker, window, around, [], [], [], note=f"{window} 未查到申报", rounds=0, calls=0, status=ResultStatus.COMPLETED)
        if self.llm is None:
            return self._envelope(ticker, window, around, chosen, [], [], note="未配置模型，只列出申报，未读取内容", rounds=0, calls=0, status=ResultStatus.PARTIAL_DATA)

        outlines = {index: self.source.outline(ref) for index, ref in enumerate(chosen, 1)}
        listing = "\n".join(
            f"申报 {index}：{ref.form} {ref.filing_date}（{ref.accession}）\n" + "\n".join(f"  - {section.id}：{section.title}（{section.chars} 字符）" for section in outlines[index])
            for index, ref in enumerate(chosen, 1)
        )
        task = f"股票：{ticker}\n关注日期：{around or '无'}\n申报窗口：{window}\n{listing}"
        messages: list[dict[str, str]] = [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": task}]
        read: dict[tuple[int, str], str] = {}
        events: list[dict[str, Any]] = []
        note = "达到轮次上限"
        rounds = calls = 0
        for _ in range(self.max_rounds):
            if time.monotonic() - started > self.max_seconds:
                note = "达到时间上限"
                break
            rounds += 1
            calls += 1
            try:
                response = self.llm.complete(messages, None)
                action = json.loads(_strip_fence(response.text))
            except Exception as exc:  # noqa: BLE001 — a bad turn is data for the envelope
                messages.append({"role": "user", "content": f"上一轮输出无法解析（{type(exc).__name__}），请只输出 JSON。"})
                continue
            messages.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)})
            if action.get("action") == "finish":
                events = [row for row in (action.get("events") or []) if isinstance(row, dict)]
                note = str(action.get("note") or "")
                break
            try:
                index = int(action.get("filing"))
                section_id = str(action.get("section") or "")
            except (TypeError, ValueError):
                messages.append({"role": "user", "content": "read 需要 filing 序号和 section id。"})
                continue
            if index not in outlines:
                messages.append({"role": "user", "content": "没有这个申报序号。"})
                continue
            text = self.source.read(chosen[index - 1], section_id)[: self.max_chars]
            read[(index, section_id)] = text
            messages.append({"role": "user", "content": f"申报 {index} 章节 {section_id} 的文本：\n{text or '（空）'}"})
        verified, dropped = _verify_events(events, read)
        if dropped:
            note = (note + "；" if note else "") + f"{dropped} 条事件的引文与已读文本不符，已丢弃"
        status = ResultStatus.COMPLETED if verified else ResultStatus.PARTIAL_DATA
        return self._envelope(ticker, window, around, chosen, verified, sorted(read), note=note, rounds=rounds, calls=calls, status=status)

    def _envelope(self, ticker: str, window: str, around: str, refs: list[FilingRef], events: list[dict[str, Any]], read: list[tuple[int, str]], *, note: str, rounds: int, calls: int, status: ResultStatus) -> ToolEnvelope:
        evidence: list[EvidenceItem] = []
        for row in events:
            ref = refs[int(row["filing"]) - 1]
            quote = str(row["quote"])
            section = str(row.get("section") or "")
            digest = hashlib.sha1(f"{ticker}|{ref.accession}|{section}|{quote}".encode("utf-8")).hexdigest()[:16]
            evidence.append(
                EvidenceItem(
                    id=f"evidence-filing-event-{digest}",
                    entity=ticker,
                    claim=f"{ticker} {row['date']}：{row['summary']}（{ref.form} {ref.filing_date} {section}：“{quote[:200]}”）",
                    as_of=str(row["date"]),
                    source_id="sec_edgar",
                    source_title=f"{ticker} {ref.form} {ref.filing_date}",
                    source_url=ref.url,
                    metadata={"evidence_scope": "filing_event", "date": str(row["date"]), "form": ref.form, "filing_date": ref.filing_date, "section": section, "quote": quote, "verified": True},
                )
            )
        limitations: list[str] = []
        if note:
            limitations.append(note)
        if not evidence:
            reason = f"{ticker} {window} 的 {len(refs)} 份申报中未读到与{around + ' 附近' if around else '该区间'}下跌相关的事件" if refs else f"{ticker} {window} 未查到申报"
            evidence.append(EvidenceItem(id=f"evidence-filing-event-none-{hashlib.sha1(f'{ticker}|{window}|{around}'.encode('utf-8')).hexdigest()[:12]}", entity=ticker, claim=reason + "。", source_id="sec_edgar", source_title="SEC EDGAR", metadata={"citation_kind": "limitations", "verified": True}))
        found = [item for item in evidence if item.metadata.get("evidence_scope") == "filing_event"]
        narrative = (f"{ticker} 申报中读到的事件：" + "；".join(f"{item.metadata['date']} {item.claim.split('：', 1)[1].split('（', 1)[0]}[{item.id}]" for item in found) + "。") if found else f"{evidence[0].claim.rstrip('。')}[{evidence[0].id}]。"
        return ToolEnvelope(
            "filings.read_events",
            status,
            subject=ticker,
            as_of=window.split(" 至 ")[-1],
            summary=f"{ticker} {window}：读了 {len(read)} 节，{len(found)} 条有出处的事件。",
            metrics={"filings": len(refs), "sections_read": len(read), "events": len(found), "rounds": rounds, "llm_calls": calls},
            evidence=evidence,
            limitations=limitations,
            metadata={"narrative": narrative, "dates": [item.metadata["date"] for item in found], "filings": [{"form": ref.form, "filing_date": ref.filing_date, "accession": ref.accession, "url": ref.url} for ref in refs], "reads": [f"{index}:{section}" for index, section in read], "around": around},
        )


def _verify_events(events: list[dict[str, Any]], read: dict[tuple[int, str], str]) -> tuple[list[dict[str, Any]], int]:
    """Keep only events whose quote appears verbatim in a section the loop actually read."""

    normalized = {key: _WS.sub(" ", text).strip().lower() for key, text in read.items()}
    kept: list[dict[str, Any]] = []
    dropped = 0
    for row in events:
        try:
            key = (int(row.get("filing")), str(row.get("section") or ""))
            quote = _WS.sub(" ", str(row.get("quote") or "")).strip()
            day = str(row.get("date") or "")[:10]
            date.fromisoformat(day)
        except (TypeError, ValueError):
            dropped += 1
            continue
        if not quote or not row.get("summary") or key not in normalized or quote.lower() not in normalized[key]:
            dropped += 1
            continue
        kept.append({**row, "date": day, "quote": quote})
    return kept, dropped


def _strip_fence(text: str) -> str:
    value = (text or "").strip()
    if value.startswith("```"):
        lines = value.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines.pop()
        value = "\n".join(lines).strip()
    start, end = value.find("{"), value.rfind("}")
    return value[start : end + 1] if start >= 0 and end > start else value


def register_filing_reader(registry: CapabilityRegistry, llm: Any, *, source: FilingSource | None = None, today_factory: Callable[[], date] = date.today, **limits: Any) -> None:
    reader = FilingReader(llm, source or EdgarFilingSource(), **limits)

    def handler(arguments: dict[str, Any], context: ExecutionContext) -> ToolEnvelope:
        return reader.run(
            str(arguments.get("ticker") or "").upper(),
            context,
            since=str(arguments.get("since") or ""),
            until=str(arguments.get("until") or ""),
            around=str(arguments.get("around") or ""),
            max_filings=int(arguments.get("max_filings") or 0) or None,
            today=today_factory(),
        )

    registry.register("filings.read_events", handler)
