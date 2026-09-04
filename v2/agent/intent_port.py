"""Dependency-free port of ``v2/bot/intent.py:classify``.

The baseline half of the comparison must run wherever the agent half runs. The
real classifier imports ``langchain_deepseek``, so in an environment with only
an LLM key installed the baseline dies with ModuleNotFoundError and there is
nothing to compare against.

Copying the prompt into this file would create the usual duplication problem:
the bot's prompt gets edited, the baseline silently measures an older one. So
the prompt and the enum whitelists are **read out of the bot's source with
ast.literal_eval** instead — no import, no third-party packages, and no drift.
If ``v2/bot/intent.py`` changes tomorrow, this port changes with it; if a
constant is renamed, extraction fails loudly rather than measuring a stale copy.

The post-processing below mirrors ``classify``'s defensive parsing exactly:
fence stripping, whitelist coercion to "unknown", numeric fallbacks, and the
bounded ``days_back``. Any divergence would make the comparison lie.
"""

from __future__ import annotations

import ast
import json
import pathlib
from typing import Any

from v2.agent.llm import LLMClient, build_llm

_INTENT_SOURCE = pathlib.Path(__file__).resolve().parents[1] / "bot" / "intent.py"

_WANTED = (
    "_SYSTEM_PROMPT",
    "_VALID_INTENTS",
    "_VALID_PNL_PERIODS",
    "_VALID_RELEASE_TYPES",
    "_INSIDER_DAYS_BACK_MIN",
    "_INSIDER_DAYS_BACK_MAX",
)

_cache: dict[str, Any] | None = None


def _literal(node: ast.AST) -> Any:
    """literal_eval, plus the one non-literal shape the bot uses: frozenset(...)."""
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "frozenset" and node.args):
            return frozenset(ast.literal_eval(node.args[0]))
        raise


def bot_intent_constants() -> dict[str, Any]:
    """Extract the live prompt and whitelists from the bot's source."""
    global _cache
    if _cache is not None:
        return _cache

    tree = ast.parse(_INTENT_SOURCE.read_text(encoding="utf-8"))
    found: dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id in _WANTED:
            found[target.id] = _literal(node.value)

    missing = [name for name in _WANTED if name not in found]
    if missing:
        raise RuntimeError(
            f"cannot read {', '.join(missing)} from {_INTENT_SOURCE} — the bot's "
            "classifier was renamed or restructured, so the baseline would be "
            "measuring a stale copy of the prompt. Update v2/agent/intent_port.py."
        )
    _cache = found
    return found


def _unknown(text: str) -> dict[str, Any]:
    return {
        "intent": "unknown", "ticker": "", "manager": "", "etf": "",
        "target_price": 0.0, "direction": "", "days_horizon": 0, "period": "",
        "days_back": 0, "release_type": "", "raw": text[:80],
    }


def classify(text: str, llm: LLMClient | None = None) -> dict[str, Any]:
    """Same contract as ``v2.bot.intent.classify``, without the LangChain import."""
    constants = bot_intent_constants()
    client = llm or build_llm()

    try:
        response = client.complete([
            {"role": "system", "content": constants["_SYSTEM_PROMPT"]},
            {"role": "user", "content": text},
        ])
    except Exception:  # noqa: BLE001 — mirrors classify's own broad guard
        return _unknown(text)

    content = (response.text or "").strip()
    if content.startswith("```"):
        lines = content.split("\n")[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        content = "\n".join(lines).strip()

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return _unknown(text)
    if not isinstance(parsed, dict):
        return _unknown(text)

    intent = str(parsed.get("intent", "")).strip()
    if intent not in constants["_VALID_INTENTS"]:
        intent = "unknown"

    try:
        target_price = float(parsed.get("target_price", 0) or 0)
    except (TypeError, ValueError):
        target_price = 0.0
    try:
        days_horizon = int(parsed.get("days_horizon", 0) or 0)
    except (TypeError, ValueError):
        days_horizon = 0

    period_raw = str(parsed.get("period", "")).strip().lower()
    period = period_raw if period_raw in constants["_VALID_PNL_PERIODS"] else ""

    try:
        days_back_raw = int(parsed.get("days_back", 0) or 0)
    except (TypeError, ValueError):
        days_back_raw = 0
    low, high = constants["_INSIDER_DAYS_BACK_MIN"], constants["_INSIDER_DAYS_BACK_MAX"]
    if days_back_raw == 0:
        days_back = 0
    else:
        days_back = max(low, min(high, days_back_raw))

    release_raw = str(parsed.get("release_type", "")).strip().lower()
    release_type = release_raw if release_raw in constants["_VALID_RELEASE_TYPES"] else ""

    return {
        "intent": intent,
        "ticker": str(parsed.get("ticker", "")).strip().upper(),
        "manager": str(parsed.get("manager", "")).strip().lower(),
        "etf": str(parsed.get("etf", "")).strip().upper(),
        "target_price": target_price,
        "direction": str(parsed.get("direction", "")).strip().lower(),
        "days_horizon": days_horizon,
        "period": period,
        "days_back": days_back,
        "release_type": release_type,
        "raw": str(parsed.get("raw", text))[:80],
    }
