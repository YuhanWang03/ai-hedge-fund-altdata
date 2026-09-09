"""The move attributor: a bounded sub-agent that explains one day's move for one stock.

Given a ticker and a date it assembles the day's facts from daily closes,
then lets the model decide what to look at: news (only when the user
allowed web search), the filings around the date (through the filing
reader, itself bounded), and the monitor's anomaly memory.  Every reason
it reports must point at something it actually fetched and quote it; a
reason supported only by memory is at most medium confidence.  What it
concludes is written back to the anomaly memory under its own id, so a
later question about the same stretch finds it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable

from v2.agent_v2.agents.base import BoundedLoop, LoopLimits, limits_for
from v2.agent_v2.agents.filing_reader import EdgarFilingSource, FilingReader, FilingSource, locate_quote
from v2.agent_v2.execution import CapabilityRegistry, ExecutionContext
from v2.agent_v2.models import EvidenceItem, ResultStatus, ToolEnvelope

_WS = re.compile(r"\s+")
_COUNT_LEAK = r"\d+\s*个(?:高置信度|候选)"
_DIRECT_CAUSE = r"主要原因|直接原因|直接驱动|由.{0,20}推动|因为|催化剂是|归因于"
_HEDGED_CAUSE = r"可能|或许|候选|低置信|中置信|尚未确认|无法确认|不能确认"
_CANDIDATE_RULE = {"forbid": _DIRECT_CAUSE, "unless": _HEDGED_CAUSE, "warning": "候选归因被表述为已确认原因"}


@dataclass
class DayFacts:
    ticker: str
    date: str
    close: float
    change: float | None
    volume: int
    average_volume_30d: float | None
    volume_ratio: float | None
    high_52w: float
    low_52w: float
    sector_etf: str = ""
    sector_return_1d: float | None = None
    relative_1d: float | None = None
    is_latest: bool = False


@dataclass
class Gathered:
    """What the loop fetched, keyed the way the finish action must refer to it."""

    news: dict[str, dict[str, Any]] = field(default_factory=dict)
    filing_events: dict[str, EvidenceItem] = field(default_factory=dict)
    filing_notes: list[EvidenceItem] = field(default_factory=list)
    memory: dict[str, dict[str, Any]] = field(default_factory=dict)
    news_calls: int = 0
    reader_calls: int = 0
    memory_calls: int = 0


def day_facts(ticker: str, day: str, prices: list[Any], sector_etf: str = "", sector_prices: list[Any] | None = None) -> DayFacts | None:
    """The day's price, move, volume ratio and 52-week range from daily bars."""

    rows = [row for row in prices if str(row.time)[:10] <= day]
    if len(rows) < 2:
        return None
    latest, previous = rows[-1], rows[-2]
    prior = rows[-31:-1]
    average = sum(float(row.volume) for row in prior) / len(prior) if prior else None
    window = rows[-252:]
    change = float(latest.close) / float(previous.close) - 1 if float(previous.close) > 0 else None
    sector_return = None
    if sector_prices:
        sector_rows = [row for row in sector_prices if str(row.time)[:10] <= str(latest.time)[:10]]
        if len(sector_rows) >= 2 and str(sector_rows[-1].time)[:10] == str(latest.time)[:10] and float(sector_rows[-2].close) > 0:
            sector_return = float(sector_rows[-1].close) / float(sector_rows[-2].close) - 1
    return DayFacts(
        ticker=ticker,
        date=str(latest.time)[:10],
        close=float(latest.close),
        change=change,
        volume=int(latest.volume),
        average_volume_30d=average,
        volume_ratio=(float(latest.volume) / average) if average else None,
        high_52w=max(float(row.close) for row in window),
        low_52w=min(float(row.close) for row in window),
        sector_etf=sector_etf,
        sector_return_1d=sector_return,
        relative_1d=(change - sector_return) if change is not None and sector_return is not None else None,
        is_latest=rows[-1] is prices[-1],
    )


_SYSTEM = """你是异动归因者，只输出 JSON，不回答用户问题。
任务：解释给定股票在给定日期的涨跌原因。你能做的动作，每轮一个：
- 搜新闻（仅在允许时）：{"action":"news","query":"英文检索词，含公司名和日期"}
- 读当日附近的申报：{"action":"filing_events"}
- 查盯盘记忆：{"action":"memory","query":"关键词"}
- 结束：{"action":"finish","reasons":[{"text":"一句中文原因","confidence":"高|中|低","source":{"kind":"news","url":"..."}或{"kind":"filing","id":"证据 id"}或{"kind":"memory","date":"YYYY-MM-DD"},"quote":"从该来源原样复制的一段原文"}],"next_steps":["..."],"note":"一句话说明结论强弱或还缺什么"}
规则：每条原因必须指向你本轮真正拿到的来源并附原文引文；只有新闻或申报原文直接、同日、幅度相称地支持时才能标"高"；仅有盯盘记忆支持的最多标"中"；市场整体波动、传闻、幅度不相称的标"低"；找不到原因就返回空 reasons 并在 note 里说明。不要编造来源。"""

_FINISH_NOW = "轮次已用完。现在只允许 finish：只报你已经拿到来源并能引用原文的原因；没有就返回空 reasons 并说明。"


class _AttributionLoop(BoundedLoop):
    def __init__(self, llm: Any, limits: LoopLimits, *, facts: DayFacts, news: Callable[[str], list[dict[str, Any]]] | None, filing_events: Callable[[], ToolEnvelope] | None, recall: Callable[[str], list[Any]] | None) -> None:
        super().__init__(llm, limits)
        self.facts = facts
        self.news = news
        self.filing_events = filing_events
        self.recall = recall
        self.gathered = Gathered()

    def handle(self, action: dict[str, Any], messages: list[dict[str, str]]) -> bool:
        kind = str(action.get("action") or "")
        if kind == "news":
            if self.news is None:
                messages.append({"role": "user", "content": "用户未授权网页搜索，news 不可用；请用 filing_events 或 memory，或直接 finish。"})
                return False
            query = str(action.get("query") or f"{self.facts.ticker} stock {self.facts.date}")
            self.gathered.news_calls += 1
            try:
                rows = list(self.news(query) or [])
            except Exception as exc:  # noqa: BLE001 — a failed search is a message, not a crash
                messages.append({"role": "user", "content": f"新闻搜索失败：{type(exc).__name__}。"})
                return True
            kept = [row for row in rows if _mentions(row, self.facts.ticker)]
            for row in kept:
                url = str(row.get("url") or "").strip()
                if url:
                    self.gathered.news[url] = row
            listing = "\n".join(f"- {row.get('published_date') or row.get('published_at') or '日期未知'} | {row.get('title') or ''} | {row.get('url')}\n  {_WS.sub(' ', str(row.get('content') or ''))[:600]}" for row in kept[:6])
            messages.append({"role": "user", "content": f"新闻搜索结果（已按是否提及 {self.facts.ticker} 过滤，{len(kept)}/{len(rows)} 条）：\n{listing or '（无）'}"})
            return True
        if kind == "filing_events":
            if self.filing_events is None:
                messages.append({"role": "user", "content": "申报阅读不可用。"})
                return False
            self.gathered.reader_calls += 1
            envelope = self.filing_events()
            lines = []
            for item in envelope.evidence:
                if item.metadata.get("evidence_scope") == "filing_event":
                    self.gathered.filing_events[item.id] = item
                    lines.append(f"- id={item.id} | {item.metadata.get('date')} | {item.claim}")
                elif item.metadata.get("citation_kind") == "limitations":
                    self.gathered.filing_notes.append(item)
                    lines.append(f"- （说明）{item.claim}")
            messages.append({"role": "user", "content": "申报阅读者的结果：\n" + ("\n".join(lines) or "（无）")})
            return True
        if kind == "memory":
            if self.recall is None:
                messages.append({"role": "user", "content": "盯盘记忆不可用。"})
                return False
            self.gathered.memory_calls += 1
            try:
                rows = list(self.recall(str(action.get("query") or f"{self.facts.ticker} 异动")) or [])
            except Exception as exc:  # noqa: BLE001
                messages.append({"role": "user", "content": f"盯盘记忆查询失败：{type(exc).__name__}。"})
                return True
            lines = []
            for row in rows[:6]:
                day = str(getattr(row, "date", "") or "")[:10]
                entry = {"date": day, "flags": str(getattr(row, "flags", "") or ""), "doc": _WS.sub(" ", str(getattr(row, "doc", "") or ""))}
                self.gathered.memory[day] = entry
                lines.append(f"- {day} | {entry['flags'] or '无标志'} | {entry['doc'][:300]}")
            messages.append({"role": "user", "content": "盯盘记忆：\n" + ("\n".join(lines) or "（无）")})
            return True
        messages.append({"role": "user", "content": "未知动作；可用：news、filing_events、memory、finish。"})
        return False


def _mentions(row: dict[str, Any], ticker: str) -> bool:
    text = f"{row.get('title') or ''} {row.get('content') or ''}".lower()
    return ticker.lower() in text


def _verify_reasons(reasons: list[dict[str, Any]], gathered: Gathered) -> tuple[list[dict[str, Any]], int]:
    kept: list[dict[str, Any]] = []
    dropped = 0
    for row in reasons:
        if not isinstance(row, dict) or not str(row.get("text") or "").strip():
            dropped += 1
            continue
        source = row.get("source") if isinstance(row.get("source"), dict) else {}
        kind = str(source.get("kind") or "")
        quote = str(row.get("quote") or "")
        level = str(row.get("confidence") or "中")
        located: str | None = None
        url = ""
        if kind == "news":
            item = gathered.news.get(str(source.get("url") or "").strip())
            if item is not None:
                url = str(item.get("url") or "")
                located = locate_quote(quote, f"{item.get('title') or ''}. {item.get('content') or ''}")
        elif kind == "filing":
            item = gathered.filing_events.get(str(source.get("id") or ""))
            if item is not None:
                url = item.source_url
                located = locate_quote(quote, f"{item.claim} {item.metadata.get('quote') or ''}")
        elif kind == "memory":
            entry = gathered.memory.get(str(source.get("date") or "")[:10])
            if entry is not None:
                located = locate_quote(quote, entry["doc"]) or entry["doc"][:200]
                if level == "高":
                    level = "中"
        if located is None or level not in {"高", "中", "低"}:
            dropped += 1
            continue
        kept.append({"text": str(row["text"]).strip(), "confidence": level, "kind": kind, "url": url, "quote": located, "source_id": str(source.get("id") or source.get("date") or url)})
    return kept, dropped


class MoveAttributor:
    """Explain one day's move with bounded, verifiable evidence; remember the result."""

    def __init__(
        self,
        llm: Any,
        *,
        price_source_factory: Callable[[], Any],
        news: Callable[[str, str], list[dict[str, Any]]] | None = None,
        filing_reader: FilingReader | None = None,
        memory_recall: Callable[[str, str, int], list[Any]] | None = None,
        memory_remember: Callable[[DayFacts, list[dict[str, Any]]], str] | None = None,
        sector_for: Callable[[str], str] | None = None,
        max_rounds: int = 5,
        max_seconds: float = 120.0,
    ) -> None:
        self.llm = llm
        self.price_source_factory = price_source_factory
        self.news = news
        self.filing_reader = filing_reader
        self.memory_recall = memory_recall
        self.memory_remember = memory_remember
        self.sector_for = sector_for
        self.max_rounds = max(1, max_rounds)
        self.max_seconds = max(5.0, max_seconds)

    def run(self, ticker: str, context: ExecutionContext, *, day: str = "", today: date | None = None) -> ToolEnvelope:
        current = today or date.today()
        source = self.price_source_factory()
        start = (current - timedelta(days=430)).isoformat()
        prices = list(source.get_prices(ticker, start, current.isoformat()) or [])
        target = (day or current.isoformat())[:10]
        sector = self.sector_for(ticker) if self.sector_for else ""
        sector_prices = list(source.get_prices(sector, start, current.isoformat()) or []) if sector and sector != ticker else []
        facts = day_facts(ticker, target, prices, sector, sector_prices)
        if facts is None:
            return ToolEnvelope("market.attribute_move", ResultStatus.FAILED, subject=ticker, errors=["no price history for that date"])
        limits = limits_for(context, max_rounds=self.max_rounds, max_seconds=self.max_seconds)
        allow_news = bool(getattr(context, "allow_web", False)) and self.news is not None
        loop = _AttributionLoop(
            self.llm,
            limits,
            facts=facts,
            news=(lambda query: self.news(query, facts.date)) if allow_news else None,
            filing_events=(lambda: self.filing_reader.run(ticker, context, around=facts.date, today=current)) if self.filing_reader is not None else None,
            recall=(lambda query: self.memory_recall(ticker, query, max(30, (current - date.fromisoformat(facts.date)).days + 30))) if self.memory_recall is not None else None,
        )
        task = (
            f"股票：{ticker}\n日期：{facts.date}\n当日涨跌：{_pct(facts.change)}，收盘 {facts.close:.2f} 美元\n"
            f"成交量：{facts.volume} 股，为 30 日均量的 {facts.volume_ratio:.2f} 倍\n" if facts.volume_ratio is not None else f"股票：{ticker}\n日期：{facts.date}\n当日涨跌：{_pct(facts.change)}，收盘 {facts.close:.2f} 美元\n"
        )
        if facts.sector_return_1d is not None:
            task += f"行业基准 {facts.sector_etf} 当日 {_pct(facts.sector_return_1d)}，相对回报 {_pct(facts.relative_1d)}\n"
        task += f"可用动作：{'news、' if allow_news else ''}{'filing_events、' if self.filing_reader is not None else ''}{'memory、' if self.memory_recall is not None else ''}finish"
        outcome = loop.run(_SYSTEM, task, finish_prompt=_FINISH_NOW)
        raw = [row for row in (outcome.final.get("reasons") or []) if isinstance(row, dict)] if outcome.finished else []
        reasons, dropped = _verify_reasons(raw, loop.gathered)
        note = str(outcome.final.get("note") or "") if outcome.finished else outcome.note
        if dropped:
            note = (note + "；" if note else "") + f"{dropped} 条原因没有可核对的来源，已丢弃"
        next_steps = [str(step) for step in (outcome.final.get("next_steps") or []) if step] if outcome.finished else []
        remembered = ""
        if self.memory_remember is not None and outcome.finished:
            try:
                remembered = self.memory_remember(facts, reasons)
            except Exception:  # noqa: BLE001 — memory is optional infrastructure
                remembered = ""
        return self._envelope(facts, reasons, loop.gathered, note=note, next_steps=next_steps, metrics={"rounds": outcome.rounds, "llm_calls": outcome.calls, "elapsed_ms": outcome.elapsed_ms, "stop_reason": outcome.stop_reason, "seconds_allowed": round(outcome.seconds_allowed, 1), "news_calls": loop.gathered.news_calls, "reader_calls": loop.gathered.reader_calls, "memory_calls": loop.gathered.memory_calls, "remembered_as": remembered}, allow_news=allow_news)

    def _envelope(self, facts: DayFacts, reasons: list[dict[str, Any]], gathered: Gathered, *, note: str, next_steps: list[str], metrics: dict[str, Any], allow_news: bool) -> ToolEnvelope:
        ticker, day = facts.ticker, facts.date
        evidence: list[EvidenceItem] = []

        def item(kind: str, claim: str, **extra: Any) -> EvidenceItem:
            digest = hashlib.sha1(f"attribute|{kind}|{ticker}|{day}|{claim}".encode("utf-8")).hexdigest()[:16]
            metadata = {"evidence_scope": kind, **extra.pop("metadata", {})}
            return EvidenceItem(id=f"evidence-attribute-{kind}-{digest}", entity=ticker, claim=claim, as_of=day, source_id=extra.pop("source_id", "market_data"), source_title=extra.pop("source_title", "Daily OHLCV market data"), metadata=metadata, **extra)

        price_claim = f"{ticker} 在 {day} 收于 {facts.close:.2f} 美元，较前一交易日 {_pct(facts.change)}。"
        evidence.append(item("price", price_claim, metric="price_change_pct", value=facts.change))
        volume = None
        if facts.volume_ratio is not None:
            volume = item("volume", f"{ticker} {day} 成交量为 {facts.volume} 股，30 日均量为 {facts.average_volume_30d:.0f} 股，量比 {facts.volume_ratio:.2f} 倍。", metric="volume_ratio", value=facts.volume_ratio)
            evidence.append(volume)
        benchmark = None
        if facts.sector_return_1d is not None:
            benchmark = item("benchmark", f"{ticker} {day} 行业基准 {facts.sector_etf} 单日回报为 {_pct(facts.sector_return_1d)}，{ticker} 相对回报为 {_pct(facts.relative_1d)}。", metadata={"benchmark": facts.sector_etf})
            evidence.append(benchmark)
        high = 0
        reason_items: list[EvidenceItem] = []
        for reason in reasons:
            confirmed = reason["confidence"] == "高"
            high += int(confirmed)
            role = "driver" if confirmed else "candidate"
            qualifier = "高置信度归因" if confirmed else f"{reason['confidence']}置信度候选解释"
            source_label = {"news": "新闻", "filing": "申报", "memory": "盯盘记忆"}.get(reason["kind"], "来源")
            claim = f"{ticker} {day} {qualifier}：{reason['text']}（{source_label}：“{reason['quote'][:160]}”）。"
            metadata = {"claim_role": "confirmed_driver" if confirmed else "candidate_driver", "causal_confidence": reason["confidence"], "driver_text": reason["text"], "note": "", "source_kind": reason["kind"], "quote": reason["quote"], "supporting_sources": [{"title": "", "url": reason["url"]}] if reason["url"] else []}
            if not confirmed:
                metadata["constraints"] = [_CANDIDATE_RULE]
            reason_items.append(item(role, claim, source_id={"news": "web_news", "filing": "sec_edgar", "memory": "anomaly_memory"}[reason["kind"]], source_title=source_label, source_url=reason["url"], confidence={"高": 0.9, "中": 0.6, "低": 0.3}[reason["confidence"]], metadata=metadata))
        evidence.extend(reason_items)
        for filing_item in gathered.filing_events.values():
            if filing_item.id not in {existing.id for existing in evidence}:
                evidence.append(filing_item)
        assessment = item("attribution", f"{ticker} {day} 异动归因中有 {high} 个高置信度直接驱动，{len(reasons) - high} 个候选解释。" + ("" if high else " 现有证据不足以确认具体触发原因。"), metric="confirmed_driver_count", value=high, source_id="move_attribution", source_title="Move attribution", metadata={"claim_role": "attribution_assessment", "verified": True})
        evidence.append(assessment)
        limitations = [note] if note else []
        if high == 0:
            limitations.append("没有高置信度的同日催化剂证据，具体触发原因尚未确认。")
        if not allow_news:
            limitations.append("用户未授权网页搜索，归因未使用新闻。")
        answer_constraints: list[dict[str, Any]] = [{"forbid": _COUNT_LEAK, "warning": "将内部归因计数直接暴露给用户"}]
        if high == 0:
            answer_constraints.append({"max_cited": {"metadata": {"claim_role": "candidate_driver"}, "max": 1, "warning": "未确认直接驱动时展示了过多弱候选线索"}})
        narrative = self._narrative(facts, evidence[0], volume, benchmark, reason_items, assessment)
        return ToolEnvelope(
            "market.attribute_move",
            ResultStatus.COMPLETED if reasons else ResultStatus.PARTIAL_DATA,
            subject=ticker,
            as_of=day,
            summary=price_claim,
            metrics={"price": facts.close, "price_change_pct": facts.change, "volume_ratio": facts.volume_ratio, "sector_etf": facts.sector_etf, "sector_return_1d": facts.sector_return_1d, "relative_1d": facts.relative_1d, "confirmed_driver_count": high, "candidate_driver_count": len(reasons) - high, **metrics},
            findings=[{"claim": reason["text"], "causal_confidence": reason["confidence"], "confirmed": reason["confidence"] == "高", "evidence_ids": [reason_item.id]} for reason, reason_item in zip(reasons, reason_items)],
            evidence=evidence,
            limitations=limitations,
            metadata={"next_steps": next_steps, "require_cited_numbers": True, "answer_constraints": answer_constraints, "narrative": narrative, "date": day},
        )

    @staticmethod
    def _narrative(facts: DayFacts, price: EvidenceItem, volume: EvidenceItem | None, benchmark: EvidenceItem | None, reasons: list[EvidenceItem], assessment: EvidenceItem) -> str:
        direction = "上涨" if (facts.change or 0) > 0 else "下跌" if (facts.change or 0) < 0 else "基本持平"
        first = f"{price.claim.rstrip('。')}[{price.id}]。"
        if volume is not None:
            first += f"{volume.claim.rstrip('。')}[{volume.id}]。"
        if benchmark is not None:
            first += f"{benchmark.claim.rstrip('。')}[{benchmark.id}]。"
        confirmed = [item for item in reasons if item.metadata.get("claim_role") == "confirmed_driver"]
        candidates = [item for item in reasons if item.metadata.get("claim_role") == "candidate_driver"]
        if confirmed:
            second = "能直接支持的高置信度驱动：" + "；".join(f"{item.metadata['driver_text']}[{item.id}]" for item in confirmed[:2]) + "。"
        else:
            second = f"“为什么{direction}”目前还不能下定论：暂未找到可核实的同日催化剂，具体触发原因尚未确认[{assessment.id}]。"
        if candidates:
            best = max(candidates, key=lambda item: float(item.confidence or 0))
            second += f"最相关的一条候选线索是“{best.metadata['driver_text']}”，只能作为排查方向[{best.id}]。"
        if benchmark is not None and facts.relative_1d is not None:
            relation = "跑赢" if facts.relative_1d > 0 else "跑输"
            third = f"从盘面看，当天{relation}行业基准 {facts.sector_etf} 约 {abs(facts.relative_1d):.2%}[{benchmark.id}]。"
        else:
            third = ""
        return "\n\n".join(part for part in (first, second, third) if part)


def _pct(value: float | None) -> str:
    return "数据不足" if value is None else f"{float(value):+.2%}"


def _default_news(query: str, day: str) -> list[dict[str, Any]]:
    import os

    from tavily import TavilyClient

    client = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])
    recent = (date.today() - date.fromisoformat(day)).days <= 7
    response = client.search(query=query, max_results=6, topic="news" if recent else "general", days=7 if recent else 400, search_depth="basic")
    return list(response.get("results", []))


def _default_recall(ticker: str, query: str, lookback_days: int) -> list[Any]:
    from v2.memory import AnomalyMemory

    return AnomalyMemory().recall(ticker, query, lookback_days=lookback_days, n_results=6)


def _default_remember(facts: DayFacts, reasons: list[dict[str, Any]]) -> str:
    from v2.memory import AnomalyMemory
    from v2.monitoring.models import Anomaly, NewsSource, ScoredReason

    anomaly = Anomaly(
        ticker=facts.ticker,
        date=facts.date,
        price=facts.close,
        price_change_pct=facts.change or 0.0,
        volume_today=facts.volume,
        volume_avg_30d=facts.average_volume_30d or 0.0,
        volume_ratio=facts.volume_ratio or 1.0,
        high_52w=facts.high_52w,
        low_52w=facts.low_52w,
        flags=["retro_attribution"],
        reasons=[ScoredReason(text=reason["text"], confidence=reason["confidence"]) for reason in reasons],
        sources=[NewsSource(title=reason["kind"], url=reason["url"]) for reason in reasons if reason["url"]],
    )
    return AnomalyMemory().remember(anomaly, doc_id=f"{facts.ticker}_{facts.date}_retro")


def _default_price_source():
    from v2.data.price_source import default_price_source

    return default_price_source()


def _default_sector(ticker: str) -> str:
    from v2.universe import sector_etf_for

    return sector_etf_for(ticker)


def register_move_attributor(
    registry: CapabilityRegistry,
    llm: Any,
    *,
    price_source_factory: Callable[[], Any] = _default_price_source,
    news: Callable[[str, str], list[dict[str, Any]]] | None = _default_news,
    filing_source: FilingSource | None = None,
    memory_recall: Callable[[str, str, int], list[Any]] | None = _default_recall,
    memory_remember: Callable[[DayFacts, list[dict[str, Any]]], str] | None = _default_remember,
    sector_for: Callable[[str], str] | None = _default_sector,
    today_factory: Callable[[], date] = date.today,
    **limits: Any,
) -> None:
    reader = FilingReader(llm, filing_source or EdgarFilingSource()) if llm is not None else None
    attributor = MoveAttributor(llm, price_source_factory=price_source_factory, news=news, filing_reader=reader, memory_recall=memory_recall, memory_remember=memory_remember, sector_for=sector_for, **limits)

    def handler(arguments: dict[str, Any], context: ExecutionContext) -> ToolEnvelope:
        return attributor.run(str(arguments.get("ticker") or "").upper(), context, day=str(arguments.get("date") or ""), today=today_factory())

    registry.register("market.attribute_move", handler)
