"""Lazy adapters for useful read-only responders that Research Engine does not replace."""

from __future__ import annotations

import hashlib
import importlib
import json
from typing import Any, Callable

from v2.agent_v2.entities import extract_entities
from v2.agent_v2.execution import CapabilityRegistry, ExecutionContext
from v2.agent_v2.models import EvidenceItem, ResultStatus, ToolEnvelope

#: Capabilities whose card lists the user's own tickers; fan-out tasks read them.
_LISTS_TICKERS = frozenset({"account.portfolio", "state.read"})


def _resolve(path: str) -> Callable[..., Any]:
    module_name, _, attr = path.rpartition(".")
    return getattr(importlib.import_module(module_name), attr)


def _text(value: Any) -> str:
    if isinstance(value, tuple):
        value = value[0]
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _wrap(capability: str, subject: str, value: Any) -> ToolEnvelope:
    content = _text(value)
    digest = hashlib.sha256(f"{capability}:{subject}:{content}".encode("utf-8")).hexdigest()[:16]
    evidence = EvidenceItem(
        id=f"legacy-{digest}",
        entity=subject,
        claim=content,
        source_id=capability,
        source_title="Existing deterministic responder",
        metadata={"legacy_formatted_output": True},
    )
    metadata = {"tickers": list(extract_entities(content))} if capability in _LISTS_TICKERS else {}
    return ToolEnvelope(
        capability,
        ResultStatus.COMPLETED,
        subject=subject,
        summary=content[:6000],
        evidence=[evidence],
        limitations=["Legacy formatted output; structured field-level evidence is not yet available."],
        metadata=metadata,
    )


def register_legacy_capabilities(registry: CapabilityRegistry) -> None:
    def call(path: str, capability: str, invoke: Callable[[Callable[..., Any], dict[str, Any]], Any], subject: Callable[[dict[str, Any]], str] = lambda _: ""):
        def handler(arguments: dict[str, Any], context: ExecutionContext) -> ToolEnvelope:
            return _wrap(capability, subject(arguments), invoke(_resolve(path), arguments))

        registry.register(capability, handler)

    call("v2.bot.responders.portfolio_view", "account.portfolio", lambda fn, args: fn(), lambda args: "portfolio")
    call("v2.bot.responders.pnl_period", "account.performance", lambda fn, args: fn({"period": args.get("period", "day")}), lambda args: "portfolio")
    call("v2.bot.responders.risk_view", "account.risk", lambda fn, args: fn({}), lambda args: "portfolio")
    call("v2.bot.responders.earnings_calendar", "account.earnings_schedule", lambda fn, args: fn({"days_horizon": args.get("days", 14)}), lambda args: "portfolio")
    call("v2.bot.responders.institutional_quick", "institutional.manager_portfolio", lambda fn, args: fn(args["manager"]), lambda args: str(args.get("manager", "")))
    call("v2.bot.responders.etf_view", "etf.ark_activity", lambda fn, args: fn(args["symbol"]), lambda args: str(args.get("symbol", "")))
    call("v2.bot.responders.macro_view", "macro.overview", lambda fn, args: fn({}), lambda args: "macro")
    call("v2.bot.responders.release_check", "macro.release", lambda fn, args: fn({"release_type": args["release_type"]}), lambda args: str(args.get("release_type", "")))

    def state_read(arguments: dict[str, Any], context: ExecutionContext) -> ToolEnvelope:
        section = str(arguments.get("section") or "watchlist")
        if section == "watchlist":
            value = _resolve("v2.bot.state.watchlist_list")()
        elif section == "alerts":
            value = _resolve("v2.bot.state.alert_list")(False)
        else:
            value = _resolve("v2.bot.state.settings_all")()
        return _wrap("state.read", section, value)

    registry.register("state.read", state_read)

    def state_mutate(arguments: dict[str, Any], context: ExecutionContext) -> ToolEnvelope:
        operation = str(arguments.get("operation") or "")
        payload = dict(arguments.get("payload") or {})
        state = importlib.import_module("v2.bot.state")
        if operation == "watchlist.add":
            ticker = str(payload.get("ticker") or "").upper()
            added = state.watchlist_add(ticker)
            message = f"已将 {ticker} 加入关注列表。" if added else f"{ticker} 已在关注列表中，无需重复添加。"
        elif operation == "watchlist.remove":
            ticker = str(payload.get("ticker") or "").upper()
            removed = state.watchlist_remove(ticker)
            message = f"已将 {ticker} 移出关注列表。" if removed else f"{ticker} 不在关注列表中。"
        elif operation == "alert.add":
            ticker = str(payload.get("ticker") or "").upper()
            direction = str(payload.get("direction") or "above")
            target = float(payload.get("target_price") or 0)
            alert_id = state.alert_add(ticker, direction, target)
            label = "涨到" if direction == "above" else "跌到"
            message = f"已设置提醒 #{alert_id}：{ticker} {label} {target:g} 美元时通知。"
        elif operation == "alert.remove":
            alert_id = int(payload.get("alert_id") or 0)
            removed = state.alert_remove(alert_id)
            message = f"已取消提醒 #{alert_id}。" if removed else f"提醒 #{alert_id} 不存在。"
        else:
            return ToolEnvelope("state.mutate", ResultStatus.FAILED, errors=[f"unsupported operation: {operation}"])
        return _wrap("state.mutate", operation, message)

    registry.register("state.mutate", state_mutate)
