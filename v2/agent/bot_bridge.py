"""Integration surface for the Telegram bot — the only file the bot must know.

Everything the bot needs is one call. The bot's own change is two lines at
``v2/bot/commands.py:934``, the ``else: # "unknown"`` dead end::

    else:  # "unknown"
        if bot_bridge.enabled():
            reply = await bot_bridge.handle_nl(text, chat_id, parsed=parsed,
                                               on_progress=progress)
            await placeholder.edit_text(reply.answer, parse_mode="HTML")
        else:
            ...existing "❓ 没听懂"...

``enabled()`` reads ``V2_AGENT_ROUTING`` and is ``False`` by default, so merging
that change alters no production behaviour until the flag is set. Rollout is the
flag's three values: off -> unknown_only -> heuristic.

Two details that matter for correctness rather than style:

* ``parsed`` is accepted from the caller. The bot has already paid for one
  classification by the time it reaches this branch; re-classifying here would
  double the cost of every routed message.
* Pronoun resolution runs *before* classification, so it benefits the fast path
  too — "那它财报呢" rewritten to "NVDA 财报呢" is answerable in one hop and
  never reaches the agent at all.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from v2.agent import router, session
from v2.agent.baseline import BaselineResult, resolve_classifier, run_baseline
from v2.agent.loop import AgentConfig, AgentResult, StepEvent, run_agent
from v2.agent.registry import ToolRegistry


def enabled() -> bool:
    """True when routing is switched on. Default off — merging is not enabling."""
    return router.routing_mode() != "off"


@dataclass
class BridgeResult:
    """What the bot needs to render, plus everything an evaluator needs to score."""

    query: str
    resolved_query: str
    decision: router.RouteDecision
    answer: str
    elapsed_ms: int
    agent: AgentResult | None = None
    baseline: BaselineResult | None = None
    resolution: session.Resolution | None = None
    parsed: dict[str, Any] = field(default_factory=dict)
    progress_log: list[str] = field(default_factory=list)

    @property
    def path(self) -> str:
        return self.decision.path

    def stats(self) -> dict[str, Any]:
        base = (self.agent or self.baseline)
        stats = dict(base.stats()) if base else {}
        stats.update({
            "path": self.path,
            "signal": self.decision.signal,
            "mode": self.decision.mode,
            "pronoun_resolved": bool(self.resolution and self.resolution.rewritten),
            "bridge_elapsed_ms": self.elapsed_ms,
        })
        return stats


# ---------------------------------------------------------------------------
# Progress rendering
# ---------------------------------------------------------------------------

_PHASE_ICON = {"plan": "🧠", "tools": "🔍", "observation": "📄",
               "repair": "⚠️", "final": "✅"}


def format_step(event: StepEvent) -> str:
    icon = _PHASE_ICON.get(event.phase, "·")
    if event.phase == "tools" and event.tools:
        return f"{icon} {event.message}\n     → {', '.join(event.tools)}"
    return f"{icon} {event.message}"


class ProgressReporter:
    """Accumulates step lines and pushes the whole block to the client.

    Telegram's ``edit_text`` replaces the message, so the caller needs the full
    text each time rather than a delta. Lines are kept so a finished run still
    shows how it got there.
    """

    def __init__(self, sink: Callable[[str], None] | None, header: str = "🤔 分析中…") -> None:
        self.sink = sink
        self.header = header
        self.lines: list[str] = []

    def __call__(self, event: StepEvent) -> None:
        self.lines.append(format_step(event))
        if self.sink is None:
            return
        self.sink(self.header + "\n\n" + "\n".join(self.lines))


def make_threadsafe_progress(
    loop: asyncio.AbstractEventLoop,
    send: Callable[[str], Any],
) -> Callable[[str], None]:
    """Adapt an async sender for use from the worker thread the loop runs on.

    ``run_agent`` is synchronous and executes in an executor thread, while
    ``placeholder.edit_text`` is a coroutine owned by the bot's event loop.
    This hands each update back across that boundary, fire-and-forget: a dropped
    progress line must never take the answer down with it.
    """
    def _push(text: str) -> None:
        try:
            asyncio.run_coroutine_threadsafe(send(text), loop)
        except Exception:  # noqa: BLE001
            pass
    return _push


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def handle_nl_sync(
    text: str,
    chat_id: int = 0,
    *,
    parsed: dict[str, Any] | None = None,
    classifier: Callable[[str], dict[str, Any]] | None = None,
    registry: ToolRegistry | None = None,
    llm: Any = None,
    config: AgentConfig | None = None,
    store: session.SessionStore | None = None,
    on_progress: Callable[[str], None] | None = None,
    mode: str | None = None,
) -> BridgeResult:
    """Resolve → classify → route → answer. Never raises."""
    started = time.time()
    store = store or session.STORE
    registry = registry or ToolRegistry()

    resolution = store.resolve(chat_id, text)
    query = resolution.text

    if parsed is None:
        classify = classifier or resolve_classifier()[0]
        try:
            parsed = classify(query)
        except Exception as exc:  # noqa: BLE001 — mirrors intent.classify's guard
            parsed = {"intent": "unknown", "ticker": "", "raw": query[:80],
                      "_error": f"{type(exc).__name__}: {exc}"}

    decision = router.route(query, parsed, mode=mode)
    reporter = ProgressReporter(on_progress)

    agent_result: AgentResult | None = None
    baseline_result: BaselineResult | None = None

    if decision.is_agent:
        agent_result = run_agent(query, llm=llm, registry=registry,
                                 config=config, on_step=reporter)
        answer = agent_result.answer
        tools_used = tuple(agent_result.trajectory.distinct_tools())
    else:
        baseline_result = run_baseline(query, classifier=lambda _t: parsed,
                                       registry=registry)
        answer = baseline_result.answer
        tools_used = (baseline_result.tool,) if baseline_result.tool else ()

    # An answer the system already knows it cannot fully verify must say so.
    # Shipping it unmarked would quietly undo the grounding guarantee: the loop
    # checked, found unsupported figures, failed to repair them — and then the
    # user reads it as if it had passed.
    if agent_result is not None and not agent_result.attribution.ok:
        answer = (f"⚠️ <i>以下回答中有数字可能被安在了错误的标的上"
                  f"（{agent_result.attribution.summary()}），请以原始数据为准。</i>"
                  f"\n\n{answer}")

    if agent_result is not None and not agent_result.grounding.ok:
        figures = ", ".join(agent_result.grounding.ungrounded[:5])
        answer = (f"⚠️ <i>以下回答中有数字未能追溯到任何工具返回"
                  f"（{figures}），请以原始数据为准。</i>\n\n{answer}")

    if resolution.rewritten:
        answer = f"<i>{resolution.note}</i>\n\n{answer}"

    store.record(chat_id, session.Turn(
        query=query,
        tickers=tuple(session.extract_tickers(query))
        or ((parsed.get("ticker"),) if parsed.get("ticker") else ()),
        tools_used=tools_used,
        answer_digest=answer[:200],
        path=decision.path,
    ))

    return BridgeResult(
        query=text,
        resolved_query=query,
        decision=decision,
        answer=answer,
        elapsed_ms=int((time.time() - started) * 1000),
        agent=agent_result,
        baseline=baseline_result,
        resolution=resolution,
        parsed=parsed,
        progress_log=reporter.lines,
    )


async def handle_nl(text: str, chat_id: int = 0, **kwargs: Any) -> BridgeResult:
    """Async wrapper — runs the synchronous pipeline off the bot's event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: handle_nl_sync(text, chat_id, **kwargs))


# ---------------------------------------------------------------------------
# Telegram entry point
# ---------------------------------------------------------------------------

#: Telegram rate-limits message edits; a 5-step run would otherwise fire five
#: edits in a couple of seconds and start getting 429s.
_PROGRESS_MIN_INTERVAL = 1.5


class _Throttled:
    """Drops progress updates that arrive faster than Telegram tolerates."""

    def __init__(self, send: Callable[[str], Any], interval: float = _PROGRESS_MIN_INTERVAL):
        self.send = send
        self.interval = interval
        self.last = 0.0

    def __call__(self, text: str) -> None:
        now = time.time()
        if now - self.last < self.interval:
            return
        self.last = now
        self.send(text)


async def _edit(placeholder: Any, text: str) -> None:
    """Edit a Telegram message, falling back to plain text on malformed HTML.

    Responder cards are hand-built HTML and safe; an agent's answer is written by
    the model and can contain a stray '<'. The bot has been bitten by exactly
    this before (see main._error_handler), and losing a correct answer to a
    parse error is the worst possible outcome, so the fallback is unconditional.
    """
    try:
        await placeholder.edit_text(text, parse_mode="HTML",
                                    disable_web_page_preview=True)
    except Exception:  # noqa: BLE001 — telegram.error.BadRequest and friends
        try:
            await placeholder.edit_text(text, disable_web_page_preview=True)
        except Exception:  # noqa: BLE001 — message unchanged / deleted / too long
            pass


async def telegram_hook(
    text: str,
    chat_id: int,
    placeholder: Any = None,
    *,
    store: session.SessionStore | None = None,
    registry: ToolRegistry | None = None,
    llm: Any = None,
    config: AgentConfig | None = None,
    classifier: Callable[[str], dict[str, Any]] | None = None,
) -> tuple[bool, dict[str, Any] | None, str]:
    """The single call ``cmd_nl`` makes. Returns (handled, parsed, resolved_text).

    ``handled`` False means "not an agent query — carry on with the existing
    dispatch", and ``parsed`` is handed back so the caller never pays for a
    second classification. ``resolved_text`` is the query after pronoun
    resolution, which the caller should use from then on.

    Placement matters: this must run **before** ``cmd_nl``'s if/elif chain, not
    inside its ``else: # unknown`` branch. That branch is only reached when the
    classifier gives up, so wiring there would make the router's other signals
    (comparison, collection, multi-topic …) unreachable — every query they are
    meant to catch classifies to *some* intent and gets dispatched earlier.
    """
    store = store or session.STORE
    loop = asyncio.get_running_loop()

    resolution = store.resolve(chat_id, text)
    query = resolution.text

    classify = classifier or resolve_classifier()[0]
    try:
        parsed = await loop.run_in_executor(None, lambda: classify(query))
    except Exception as exc:  # noqa: BLE001 — mirrors intent.classify's own guard
        parsed = {"intent": "unknown", "ticker": "", "raw": query[:80],
                  "_error": f"{type(exc).__name__}: {exc}"}

    # Remembered whichever path answers, so the *next* turn can resolve "它".
    store.record(chat_id, session.Turn(
        query=query,
        tickers=tuple(session.extract_tickers(query))
        or ((parsed.get("ticker"),) if parsed.get("ticker") else ()),
    ))

    decision = router.route(query, parsed)
    if not decision.is_agent:
        return False, parsed, query

    progress: Callable[[str], None] | None = None
    if placeholder is not None:
        sender = make_threadsafe_progress(loop, lambda t: _edit(placeholder, t))
        progress = _Throttled(sender)

    result = await loop.run_in_executor(None, lambda: handle_nl_sync(
        query, chat_id, parsed=parsed, registry=registry, llm=llm,
        config=config, store=store, on_progress=progress,
        mode=decision.mode))

    if placeholder is not None:
        chip = f"<i>{router.explain(decision)}</i>\n\n"
        await _edit(placeholder, chip + result.answer)

    return True, parsed, query
