"""Lazy adapters for useful read-only responders that Research Engine does not replace."""

from __future__ import annotations

import hashlib
import importlib
import json
from typing import Any, Callable

from v2.agent_v2.execution import CapabilityRegistry, ExecutionContext
from v2.agent_v2.models import EvidenceItem, ResultStatus, ToolEnvelope


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
    return ToolEnvelope(
        capability,
        ResultStatus.COMPLETED,
        subject=subject,
        summary=content[:6000],
        evidence=[evidence],
        limitations=["Legacy formatted output; structured field-level evidence is not yet available."],
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
