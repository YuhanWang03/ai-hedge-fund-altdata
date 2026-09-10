"""A run ledger for the sub-agents, and the report that reads it.

Every live run appends one JSON line per sub-agent run (the reader, the
attributor with its nested reader runs, the news checker): rounds, calls,
elapsed time, stop reason and yield (what it reported versus what the
verifier dropped).  Token cost per sub-agent comes from the usage ledger,
where each provider call is attributed to ``agent_v2.<name>`` while the
sub-agent's loop runs.  ``python -m v2.agent_v2.eval.subagent_report``
aggregates both.
"""

from __future__ import annotations

import json
import logging
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from v2.agent_v2.models import AgentResult, sub_agent_summaries

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_PATH = _PROJECT_ROOT / "data" / "agent_v2_subagents.jsonl"


def ledger_path() -> Path:
    return Path(os.environ.get("AGENT_V2_SUBAGENT_LEDGER") or _DEFAULT_PATH)


def rows_for(result: AgentResult, *, channel: str = "") -> list[dict[str, Any]]:
    """One ledger row per sub-agent run in ``result`` (nested reader runs included, flagged as such)."""

    at = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    question = (result.request.original_text or result.request.text or "")[:80]
    rows: list[dict[str, Any]] = []
    for entry in sub_agent_summaries(result.results):
        base = {"at": at, "run_id": result.run_id, "channel": channel or str(result.request.metadata.get("channel") or ""), "question": question, "status": result.status.value}
        rows.append({**base, "agent": entry["name"], "capability": entry["capability"], "subject": entry["subject"], "rounds": entry["rounds"], "llm_calls": entry["llm_calls"], "elapsed_ms": entry["elapsed_ms"], "seconds_allowed": entry["seconds_allowed"], "stop_reason": entry["stop_reason"], "calls": entry["calls"], "yield": entry["yield"], "intraday": entry["intraday"], "nested": False})
        for nested in entry["nested"]:
            rows.append({**base, "agent": "filing_reader", "capability": entry["capability"], "subject": entry["subject"], "rounds": nested["rounds"], "llm_calls": None, "elapsed_ms": nested["elapsed_ms"], "seconds_allowed": None, "stop_reason": nested["stop_reason"], "calls": nested["calls"], "yield": {"kept": nested["calls"].get("events"), "dropped": None}, "intraday": entry["intraday"], "nested": True})
    return rows


def record_runs(result: AgentResult, *, channel: str = "", path: Path | None = None) -> int:
    """Append the run's sub-agent rows; best effort, never raises. Returns the rows written."""

    rows = rows_for(result, channel=channel)
    if not rows:
        return 0
    target = path or ledger_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("sub-agent ledger not written (%s): %s", target, exc)
        return 0
    return len(rows)


def read_rows(path: Path | None = None, *, since_days: int | None = None) -> list[dict[str, Any]]:
    target = path or ledger_path()
    if not target.exists():
        return []
    cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=since_days)).isoformat() if since_days else ""
    rows: list[dict[str, Any]] = []
    with target.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and (not cutoff or str(row.get("at") or "") >= cutoff):
                rows.append(row)
    return rows


def _percentile(values: list[float], share: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(share * (len(ordered) - 1)))))
    return ordered[index]


def aggregate(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per agent: runs, rounds, time, stop reasons, calls, yield and drop rate."""

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get("agent") or "?")].append(row)
    summary: dict[str, dict[str, Any]] = {}
    for agent, items in sorted(groups.items()):
        rounds = [float(item.get("rounds") or 0) for item in items]
        elapsed = [float(item.get("elapsed_ms") or 0) / 1000 for item in items]
        stops = Counter(str(item.get("stop_reason") or "?") for item in items)
        calls: Counter = Counter()
        for item in items:
            for key, value in (item.get("calls") or {}).items():
                if isinstance(value, (int, float)):
                    calls[key] += value
        kept = sum(int((item.get("yield") or {}).get("kept") or 0) for item in items)
        dropped = sum(int((item.get("yield") or {}).get("dropped") or 0) for item in items)
        confirmed = sum(int((item.get("yield") or {}).get("confirmed") or 0) for item in items)
        reported = kept + dropped
        summary[agent] = {
            "runs": len(items),
            "rounds_avg": round(sum(rounds) / len(rounds), 2) if rounds else 0.0,
            "seconds_avg": round(sum(elapsed) / len(elapsed), 1) if elapsed else 0.0,
            "seconds_p90": round(_percentile(elapsed, 0.9), 1),
            "stop_reasons": dict(stops),
            "calls": dict(calls),
            "kept": kept,
            "dropped": dropped,
            "confirmed": confirmed,
            "drop_rate": round(dropped / reported, 3) if reported else None,
            "kept_per_run": round(kept / len(items), 2) if items else 0.0,
            "empty_runs": sum(1 for item in items if not int((item.get("yield") or {}).get("kept") or 0)),
        }
    return summary


def usage_by_source(since_days: int | None = None) -> dict[str, dict[str, Any]]:
    """Token and cost totals per ``agent_v2.*`` source from the usage ledger; empty when the ledger is unavailable."""

    try:
        from v2.data.cost_ledger import _conn
    except Exception:  # noqa: BLE001 — the ledger is optional infrastructure
        return {}
    cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=since_days)).isoformat() if since_days else ""
    totals: dict[str, dict[str, Any]] = defaultdict(lambda: {"calls": 0, "input_tokens": 0.0, "output_tokens": 0.0, "cost_usd": 0.0, "failed": 0})
    try:
        with _conn() as conn:
            query = "SELECT payload, cost_usd FROM usage_events WHERE category='llm'" + (" AND occurred_at>=?" if cutoff else "")
            for payload, cost in conn.execute(query, (cutoff,) if cutoff else ()):
                try:
                    event = json.loads(payload)
                except (TypeError, json.JSONDecodeError):
                    continue
                source = str(event.get("source") or "")
                if not source.startswith("agent_v2."):
                    continue
                bucket = totals[source]
                bucket["calls"] += 1
                usage = event.get("usage") or {}
                bucket["input_tokens"] += float(usage.get("input_tokens") or 0)
                bucket["output_tokens"] += float(usage.get("output_tokens") or 0)
                bucket["cost_usd"] += float(cost or 0)
                if event.get("state") != "success":
                    bucket["failed"] += 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("usage ledger unavailable for the sub-agent report: %s", exc)
        return {}
    return {source: {**bucket, "cost_usd": round(bucket["cost_usd"], 4)} for source, bucket in sorted(totals.items())}


def render(summary: dict[str, dict[str, Any]], usage: dict[str, dict[str, Any]], *, since_days: int | None) -> str:
    lines = [f"# 子智能体运行报告{f'（近 {since_days} 天）' if since_days else ''}", ""]
    if not summary:
        lines.append("账本里还没有子智能体运行记录。")
    else:
        lines.append("| 子智能体 | 运行 | 平均轮次 | 平均秒 | P90 秒 | 停止原因 | 产出/次 | 丢弃率 | 空跑 |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for agent, row in summary.items():
            stops = "、".join(f"{key} {value}" for key, value in sorted(row["stop_reasons"].items()))
            drop = "—" if row["drop_rate"] is None else f"{row['drop_rate']:.0%}"
            lines.append(f"| {agent} | {row['runs']} | {row['rounds_avg']} | {row['seconds_avg']} | {row['seconds_p90']} | {stops} | {row['kept_per_run']} | {drop} | {row['empty_runs']} |")
        lines.append("")
        lines.append("产出 = 校验后保留的原因或事件数；丢弃率 = 被校验器丢弃的占报出总数的比例；空跑 = 一条都没保留的运行。")
    lines.append("")
    if usage:
        lines.append("| 来源 | 模型调用 | 输入 tokens | 输出 tokens | 估算成本 USD | 失败 |")
        lines.append("|---|---|---|---|---|---|")
        for source, row in usage.items():
            lines.append(f"| {source} | {row['calls']} | {int(row['input_tokens'])} | {int(row['output_tokens'])} | {row['cost_usd']:.4f} | {row['failed']} |")
    else:
        lines.append("用量账本不可用或没有 agent_v2.* 的记录（Token 与成本按来源归属需要线上账本）。")
    return "\n".join(lines)
