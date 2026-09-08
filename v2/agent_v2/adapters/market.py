"""Structured market-performance and move-attribution adapters for Agent V2."""

from __future__ import annotations

import hashlib
import math
from datetime import date, timedelta
from typing import Any, Callable

from v2.agent_v2.execution import CapabilityRegistry, ExecutionContext
from v2.agent_v2.models import EvidenceItem, ResultStatus, ToolEnvelope


def _default_price_source():
    from v2.data.price_source import default_price_source

    return default_price_source()


def _default_move_provider(ticker: str):
    from v2.bot.responders import _build_query_anomaly
    from v2.data import CachedFDClient
    from v2.memory import AnomalyMemory
    from v2.monitoring import attribute

    with CachedFDClient() as fd:
        anomaly = _build_query_anomaly(ticker, fd)
        if anomaly is None:
            return None
        try:
            memory = AnomalyMemory()
        except Exception:
            memory = None
        return attribute(anomaly, fd_client=fd, memory=memory)


def _return(prices, window: int) -> float | None:
    if len(prices) <= window or float(prices[-1 - window].close) <= 0:
        return None
    return float(prices[-1].close) / float(prices[-1 - window].close) - 1


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _evidence_id(kind: str, ticker: str, as_of: str, claim: str) -> str:
    digest = hashlib.sha1(f"{kind}|{ticker}|{as_of}|{claim}".encode("utf-8")).hexdigest()[:16]
    return f"evidence-market-{kind}-{digest}"


def _item(
    kind: str,
    ticker: str,
    as_of: str,
    claim: str,
    context: ExecutionContext,
    *,
    metric: str = "",
    value: Any = None,
    confidence: float = 1.0,
    source_url: str = "",
    metadata: dict[str, Any] | None = None,
) -> EvidenceItem:
    return EvidenceItem(
        id=_evidence_id(kind, ticker, as_of, claim),
        entity=ticker,
        claim=claim,
        metric=metric,
        value=value,
        as_of=as_of,
        source_id="market_data" if kind not in {"driver", "candidate"} else "move_attribution",
        source_title="Daily OHLCV market data" if kind not in {"driver", "candidate"} else "Move attribution evidence",
        source_url=source_url,
        confidence=confidence,
        producer_run_id=context.run_id,
        metadata={"evidence_scope": kind, **(metadata or {})},
    )


def _performance_envelope(ticker: str, context: ExecutionContext, price_source) -> ToolEnvelope:
    today = date.today()
    start = today - timedelta(days=430)
    prices = price_source.get_prices(ticker, start.isoformat(), today.isoformat()) or []
    if len(prices) < 2:
        return ToolEnvelope("market.performance", ResultStatus.FAILED, subject=ticker, errors=["no recent price history"])

    latest, previous = prices[-1], prices[-2]
    as_of = str(latest.time)[:10]
    close = float(latest.close)
    day_return = close / float(previous.close) - 1 if float(previous.close) > 0 else None
    windows = {"1d": day_return, "5d": _return(prices, 5), "1m": _return(prices, 21), "3m": _return(prices, 63), "1y": _return(prices, 252)}
    volumes = [float(row.volume) for row in prices[-31:-1] if _finite(row.volume) is not None]
    avg_volume_30d = sum(volumes) / len(volumes) if volumes else None
    volume_ratio = float(latest.volume) / avg_volume_30d if avg_volume_30d else None
    recent_returns = [float(prices[i].close) / float(prices[i - 1].close) - 1 for i in range(max(1, len(prices) - 21), len(prices)) if float(prices[i - 1].close) > 0]
    volatility_21d = (sum((value - sum(recent_returns) / len(recent_returns)) ** 2 for value in recent_returns) / max(1, len(recent_returns) - 1)) ** 0.5 * (252**0.5) if len(recent_returns) > 1 else None

    evidence: list[EvidenceItem] = []
    price_claim = f"{ticker} 截至 {as_of} 收盘价为 {close:.2f} 美元，单日涨跌幅为 {day_return:+.2%}。" if day_return is not None else f"{ticker} 截至 {as_of} 收盘价为 {close:.2f} 美元。"
    evidence.append(_item("price", ticker, as_of, price_claim, context, metric="close", value=close, metadata={"day_return": day_return}))
    available_windows = {key: value for key, value in windows.items() if value is not None}
    window_claim = f"{ticker} 区间回报：" + "，".join(f"{key} {value:+.2%}" for key, value in available_windows.items()) + "。"
    evidence.append(_item("returns", ticker, as_of, window_claim, context, metadata={"returns": available_windows}))
    if avg_volume_30d is not None and volume_ratio is not None:
        volume_claim = f"{ticker} {as_of} 成交量为 {int(latest.volume)} 股，30 日平均成交量为 {avg_volume_30d:.0f} 股，量比为 {volume_ratio:.2f} 倍。"
        evidence.append(_item("volume", ticker, as_of, volume_claim, context, metric="volume_ratio", value=volume_ratio, metadata={"volume": int(latest.volume), "average_volume_30d": avg_volume_30d}))
    if volatility_21d is not None:
        volatility_claim = f"{ticker} 近 21 个交易日的实现波动率折算年化后为 {volatility_21d:.2%}。"
        evidence.append(_item("volatility", ticker, as_of, volatility_claim, context, metric="annualized_volatility_21d", value=volatility_21d))

    from v2.universe import BENCHMARK_ETF, sector_etf_for

    sector_etf = sector_etf_for(ticker)
    relative: dict[str, dict[str, float | None]] = {}
    limitations: list[str] = []
    for benchmark in dict.fromkeys((sector_etf, BENCHMARK_ETF)):
        benchmark_prices = price_source.get_prices(benchmark, start.isoformat(), today.isoformat()) or []
        benchmark_windows = {key: _return(benchmark_prices, window) for key, window in (("1d", 1), ("5d", 5), ("1m", 21))}
        comparable = {key: value for key, value in benchmark_windows.items() if value is not None and windows.get(key) is not None}
        if not comparable:
            limitations.append(f"{benchmark} benchmark history unavailable")
            continue
        relative[benchmark] = {key: windows[key] - value for key, value in comparable.items()}
        claim = f"同期基准 {benchmark} 回报：" + "，".join(f"{key} {value:+.2%}（{ticker} 相对 {windows[key] - value:+.2%}）" for key, value in comparable.items()) + "。"
        evidence.append(_item("benchmark", ticker, as_of, claim, context, metadata={"benchmark": benchmark, "benchmark_returns": comparable, "relative_returns": relative[benchmark]}))

    metrics = {
        "close": close,
        "returns": available_windows,
        "volume": int(latest.volume),
        "average_volume_30d": avg_volume_30d,
        "volume_ratio": volume_ratio,
        "annualized_volatility_21d": volatility_21d,
        "sector_benchmark": sector_etf,
        "relative_returns": relative,
    }
    summary = price_claim + " " + window_claim
    status = ResultStatus.COMPLETED if "1m" in available_windows and relative else ResultStatus.PARTIAL_DATA
    return ToolEnvelope("market.performance", status, subject=ticker, as_of=as_of, summary=summary, metrics=metrics, evidence=evidence, limitations=limitations)


def _move_envelope(ticker: str, context: ExecutionContext, anomaly) -> ToolEnvelope:
    if anomaly is None:
        return ToolEnvelope("market.explain_move", ResultStatus.FAILED, subject=ticker, errors=["no recent move data"])
    as_of = str(anomaly.date)[:10]
    evidence: list[EvidenceItem] = []
    price_claim = f"{ticker} 在 {as_of} 收于 {float(anomaly.price):.2f} 美元，较前一交易日 {float(anomaly.price_change_pct):+.2%}。"
    evidence.append(_item("price", ticker, as_of, price_claim, context, metric="price_change_pct", value=float(anomaly.price_change_pct), metadata={"close": float(anomaly.price)}))
    volume_claim = f"{ticker} 当日成交量为 {int(anomaly.volume_today)} 股，30 日均量为 {float(anomaly.volume_avg_30d):.0f} 股，量比 {float(anomaly.volume_ratio):.2f} 倍。"
    evidence.append(_item("volume", ticker, as_of, volume_claim, context, metric="volume_ratio", value=float(anomaly.volume_ratio)))
    if anomaly.sector_etf and anomaly.sector_return_1d is not None:
        benchmark_claim = f"同期行业基准 {anomaly.sector_etf} 单日回报为 {float(anomaly.sector_return_1d):+.2%}，{ticker} 相对回报为 {float(anomaly.relative_1d_pp or 0):+.2%}。"
        evidence.append(_item("benchmark", ticker, as_of, benchmark_claim, context, metadata={"benchmark": anomaly.sector_etf, "contrarian": bool(anomaly.contrarian)}))

    findings: list[dict[str, Any]] = []
    source_rows = [source.model_dump() if hasattr(source, "model_dump") else dict(source) for source in anomaly.sources]
    high_confidence = 0
    confidence_value = {"高": 0.9, "中": 0.6, "低": 0.3}
    for reason in anomaly.reasons:
        level = str(reason.confidence)
        confirmed = level == "高"
        high_confidence += int(confirmed)
        role = "driver" if confirmed else "candidate"
        qualifier = "高置信度归因" if confirmed else f"{level}置信度候选解释"
        note = f"；校验备注：{reason.note}" if reason.note else ""
        claim = f"{ticker} {qualifier}：{reason.text}{note}。"
        # Attribution currently returns a shared source set, not a reason-to-source
        # mapping. Only expose a direct URL when the relationship is unambiguous.
        source = source_rows[0] if len(source_rows) == 1 else {}
        item = _item(role, ticker, as_of, claim, context, confidence=confidence_value.get(level, 0.3), source_url=str(source.get("url") or ""), metadata={"claim_role": "confirmed_driver" if confirmed else "candidate_driver", "causal_confidence": level, "note": reason.note, "supporting_sources": source_rows})
        evidence.append(item)
        findings.append({"claim": reason.text, "causal_confidence": level, "confirmed": confirmed, "evidence_ids": [item.id]})

    limitations: list[str] = []
    if high_confidence == 0:
        limitations.append("没有高置信度的同日催化剂证据，具体触发原因尚未确认。")
    if not source_rows:
        limitations.append("没有可用于归因的已验证同日新闻来源。")
    metrics = {
        "price": float(anomaly.price),
        "price_change_pct": float(anomaly.price_change_pct),
        "volume_ratio": float(anomaly.volume_ratio),
        "sector_etf": anomaly.sector_etf,
        "sector_return_1d": anomaly.sector_return_1d,
        "relative_1d": anomaly.relative_1d_pp,
        "confirmed_driver_count": high_confidence,
        "candidate_driver_count": len(anomaly.reasons) - high_confidence,
    }
    assessment_claim = (
        f"{ticker} 异动归因中有 {high_confidence} 个高置信度直接驱动，"
        f"{len(anomaly.reasons) - high_confidence} 个候选解释。"
    )
    if high_confidence == 0:
        assessment_claim += " 现有证据不足以确认具体触发原因。"
    evidence.append(
        _item(
            "attribution",
            ticker,
            as_of,
            assessment_claim,
            context,
            metric="confirmed_driver_count",
            value=high_confidence,
            metadata={
                "claim_role": "attribution_assessment",
                "candidate_driver_count": len(anomaly.reasons) - high_confidence,
            },
        )
    )
    return ToolEnvelope(
        "market.explain_move",
        ResultStatus.COMPLETED if evidence else ResultStatus.PARTIAL_DATA,
        subject=ticker,
        as_of=as_of,
        summary=price_claim,
        metrics=metrics,
        findings=findings,
        evidence=evidence,
        limitations=limitations,
        metadata={"next_steps": list(anomaly.next_steps), "filtered_news_count": int(anomaly.filtered_count), "source_count": len(source_rows)},
    )


def register_market_capabilities(
    registry: CapabilityRegistry,
    *,
    price_source_factory: Callable[[], Any] = _default_price_source,
    move_provider: Callable[[str], Any] = _default_move_provider,
) -> None:
    def performance(arguments: dict[str, Any], context: ExecutionContext) -> ToolEnvelope:
        ticker = str(arguments.get("ticker") or "").upper()
        return _performance_envelope(ticker, context, price_source_factory())

    def explain(arguments: dict[str, Any], context: ExecutionContext) -> ToolEnvelope:
        ticker = str(arguments.get("ticker") or "").upper()
        return _move_envelope(ticker, context, move_provider(ticker))

    registry.register("market.performance", performance)
    registry.register("market.explain_move", explain)
