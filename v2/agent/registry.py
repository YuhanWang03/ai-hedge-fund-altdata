"""Tool registry — the 24 responders exposed as a model-callable tool surface.

Why this file exists
--------------------
``v2/bot/commands.py`` already routes 24 intents to 24 responders, but it does
so through a hand-written if/elif chain: the *code* decides which responder
runs, exactly once, and the LLM only picks a label. To let a model drive the
control flow instead, the same responders have to be described in a way a model
can read (JSON Schema) and invoked in a way that never crashes the caller.

Two design rules follow from that:

1. **Import-time purity.** Specs are plain data holding a *dotted path*, not a
   function object. Nothing under ``v2.bot`` is imported until a tool actually
   fires. That keeps this module importable in a bare sandbox (no numpy, no
   telegram) and keeps the bot's startup cost unchanged.

2. **Errors are values, not exceptions.** ``ToolRegistry.call`` always returns a
   ``ToolResult``. In the bot, a responder raising bubbles up to
   ``main._error_handler`` and the *user* sees the traceback class. In an agent
   loop the *model* has to see it, so it can pick a different route — that is
   the whole point of a feedback loop.

The registry is also the enforcement point for mutations. Three tools write to
``state.db`` (watchlist add/remove, alert set/remove). An autonomous loop that
can write on its own initiative is a different risk class from one that can
only read, so writes are opt-in per run via ``allow_mutations``.
"""

from __future__ import annotations

import importlib
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal


InvokeStyle = Literal["none", "dict", "args"]


@dataclass(frozen=True)
class ToolSpec:
    """One model-callable tool, described without importing its implementation.

    Attributes:
        name: the identifier the model emits in a tool call.
        description: what the model reads when choosing. Written to disambiguate
            against its neighbours — a tool description competing with 23 others
            is a retrieval problem, not a documentation one.
        parameters: JSON Schema (object) for the arguments.
        target: dotted path to the callable, resolved on first use.
        invoke_style: how ``parameters`` map onto the callable's signature.
            "none" -> fn(); "dict" -> fn(args); "args" -> fn(*ordered values).
        arg_order: for invoke_style="args", the property order to pass.
        mutating: True if the call writes to state.db.
        cost_hint: rough seconds; used only for budgeting/telemetry display.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    target: str
    invoke_style: InvokeStyle = "dict"
    arg_order: tuple[str, ...] = ()
    mutating: bool = False
    cost_hint: float = 2.0

    def to_openai_schema(self) -> dict[str, Any]:
        """Render as an OpenAI-compatible ``tools[]`` entry."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolResult:
    """Outcome of one tool call. ``ok=False`` is still a legal observation."""

    name: str
    args: dict[str, Any]
    ok: bool
    content: str
    elapsed_ms: int = 0
    error_kind: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def as_observation(self) -> str:
        """Text handed back to the model. Failures stay actionable, not opaque."""
        if self.ok:
            return self.content
        return f"[TOOL_ERROR {self.error_kind}] {self.content}"


# ---------------------------------------------------------------------------
# Schema fragments
# ---------------------------------------------------------------------------

_TICKER = {"type": "string", "description": "US ticker symbol, e.g. NVDA"}
_EMPTY: dict[str, Any] = {"type": "object", "properties": {}}


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or []}


# ---------------------------------------------------------------------------
# The 24 tools — same capability surface as the 24 bot intents
# ---------------------------------------------------------------------------

TOOL_SPECS: tuple[ToolSpec, ...] = (
    # --- account / portfolio -------------------------------------------------
    ToolSpec(
        name="portfolio_view",
        description=(
            "List every current position in the user's Alpaca account: ticker, "
            "quantity, market value, weight, unrealized P&L. Start here for any "
            "question about what the user actually holds."
        ),
        parameters=_EMPTY,
        target="v2.bot.responders.portfolio_view",
        invoke_style="none",
        cost_hint=1.5,
    ),
    ToolSpec(
        name="pnl_view",
        description="Today's account-level P&L and total equity. Same-day only.",
        parameters=_EMPTY,
        target="v2.bot.responders.pnl_view",
        invoke_style="none",
        cost_hint=1.5,
    ),
    ToolSpec(
        name="pnl_period",
        description=(
            "Account P&L over a period (day / week / month). Use this, not "
            "pnl_view, whenever the question spans more than today."
        ),
        parameters=_obj(
            {"period": {"type": "string", "enum": ["day", "week", "month"]}},
            ["period"],
        ),
        target="v2.bot.responders.pnl_period",
        cost_hint=2.0,
    ),
    ToolSpec(
        name="risk_view",
        description=(
            "Portfolio-level risk panorama: concentration, sector exposure, "
            "drawdown, and which holdings report earnings within 7 days. Returns "
            "aggregate risk for the whole book — it does NOT rank individual "
            "positions, so combine it with per-ticker tools for that."
        ),
        parameters=_EMPTY,
        target="v2.bot.responders.risk_view",
        invoke_style="dict",
        cost_hint=3.0,
    ),
    # --- per-ticker analysis -------------------------------------------------
    ToolSpec(
        name="explain_move",
        description=(
            "Explain WHY one ticker moved recently: price change, volume, "
            "sector-relative strength, and Tier-1/2 news attribution. Use for "
            "'why is X up/down' questions."
        ),
        parameters=_obj({"ticker": _TICKER}, ["ticker"]),
        target="v2.bot.responders.explain_move",
        invoke_style="args",
        arg_order=("ticker",),
        cost_hint=6.0,
    ),
    ToolSpec(
        name="summary",
        description=(
            "Broad overview of one ticker: price, fundamentals, latest earnings, "
            "recent news. Use for open-ended 'tell me about X' questions."
        ),
        parameters=_obj({"ticker": _TICKER}, ["ticker"]),
        target="v2.bot.responders.summary",
        invoke_style="args",
        arg_order=("ticker",),
        cost_hint=8.0,
    ),
    ToolSpec(
        name="chain",
        description=(
            "Supply-chain / peer expansion for one ticker: upstream, downstream "
            "and comparable names with the reason for each link."
        ),
        parameters=_obj({"ticker": _TICKER}, ["ticker"]),
        target="v2.bot.responders.chain",
        invoke_style="args",
        arg_order=("ticker",),
        cost_hint=7.0,
    ),
    ToolSpec(
        name="moneyflow_view",
        description=(
            "Money-flow divergence for one ticker: CMF and RSI against price, to "
            "judge accumulation vs distribution."
        ),
        parameters=_obj({"ticker": _TICKER}, ["ticker"]),
        target="v2.bot.responders.moneyflow_view",
        cost_hint=4.0,
    ),
    # --- earnings ------------------------------------------------------------
    ToolSpec(
        name="earnings_view",
        description=(
            "Earnings detail for ONE ticker: next report date, last quarter's "
            "beat/miss and surprise."
        ),
        parameters=_obj({"ticker": _TICKER}, ["ticker"]),
        target="v2.bot.responders.earnings_view",
        cost_hint=3.0,
    ),
    ToolSpec(
        name="earnings_calendar",
        description=(
            "Upcoming earnings across the user's watchlist and holdings within N "
            "days. Use when the question is about scheduling, not one company."
        ),
        parameters=_obj(
            {"days_horizon": {"type": "integer", "minimum": 1, "maximum": 90,
                              "description": "Look-ahead window in days; default 14"}}
        ),
        target="v2.bot.responders.earnings_calendar",
        cost_hint=3.0,
    ),
    # --- SEC -----------------------------------------------------------------
    ToolSpec(
        name="eight_k_view",
        description=(
            "SEC 8-K filings for one ticker over the last 30 days — material "
            "events (item 5.02 departures, 2.02 results, 1.01 agreements)."
        ),
        parameters=_obj({"ticker": _TICKER}, ["ticker"]),
        target="v2.bot.responders.eight_k_view",
        cost_hint=4.0,
    ),
    ToolSpec(
        name="insider_view",
        description=(
            "SEC Form 4 insider transactions for one ticker: who bought or sold, "
            "how much, and whether it clusters."
        ),
        parameters=_obj(
            {"ticker": _TICKER,
             "days_back": {"type": "integer", "minimum": 7, "maximum": 365,
                           "description": "Look-back window; default 90"}},
            ["ticker"],
        ),
        target="v2.bot.responders.insider_view",
        cost_hint=4.0,
    ),
    # --- institutional / ETF -------------------------------------------------
    ToolSpec(
        name="institutional_13f",
        description=(
            "Latest 13F portfolio for a named institutional manager (buffett, "
            "burry, ackman, ark, citadel, ...). Quarterly and lagged."
        ),
        parameters=_obj(
            {"manager": {"type": "string",
                         "description": "Manager alias, e.g. 'buffett' or 'burry'"}},
            ["manager"],
        ),
        target="v2.bot.responders.institutional_quick",
        invoke_style="args",
        arg_order=("manager",),
        cost_hint=6.0,
    ),
    ToolSpec(
        name="holders",
        description="Which institutions hold a given ticker, by latest 13F.",
        parameters=_obj({"ticker": _TICKER}, ["ticker"]),
        target="v2.bot.responders.holders",
        invoke_style="args",
        arg_order=("ticker",),
        cost_hint=5.0,
    ),
    ToolSpec(
        name="etf_view",
        description=(
            "Daily holdings snapshot for an ARK ETF (ARKK/ARKQ/ARKG/ARKW/ARKF), "
            "including the most recent buys and sells."
        ),
        parameters=_obj(
            {"symbol": {"type": "string", "description": "ARK ETF symbol, e.g. ARKK"}},
            ["symbol"],
        ),
        target="v2.bot.responders.etf_view",
        invoke_style="args",
        arg_order=("symbol",),
        cost_hint=4.0,
    ),
    # --- macro ---------------------------------------------------------------
    ToolSpec(
        name="macro_view",
        description=(
            "Macro dashboard: VIX, DXY, WTI, gold, treasury yields and the most "
            "recent economic releases."
        ),
        parameters=_EMPTY,
        target="v2.bot.responders.macro_view",
        cost_hint=3.0,
    ),
    ToolSpec(
        name="release_check",
        description=(
            "Latest reading for one macro release series (cpi, pce, nfp, gdp, "
            "ppi, claims, fomc) with its prior value and release date."
        ),
        parameters=_obj(
            {"release_type": {"type": "string",
                              "enum": ["cpi", "pce", "nfp", "gdp", "ppi", "claims", "fomc"]}},
            ["release_type"],
        ),
        target="v2.bot.responders.release_check",
        cost_hint=2.0,
    ),
    # --- watchlist / alerts / settings ---------------------------------------
    ToolSpec(
        name="watchlist_view",
        description="The user's watchlist (tickers they track but may not hold).",
        parameters=_EMPTY,
        target="v2.bot.state.watchlist_list",
        invoke_style="none",
        cost_hint=0.1,
    ),
    ToolSpec(
        name="watchlist_add",
        description="Add a ticker to the watchlist. Writes to the database.",
        parameters=_obj({"ticker": _TICKER}, ["ticker"]),
        target="v2.bot.state.watchlist_add",
        invoke_style="args",
        arg_order=("ticker",),
        mutating=True,
        cost_hint=0.1,
    ),
    ToolSpec(
        name="watchlist_remove",
        description="Remove a ticker from the watchlist. Writes to the database.",
        parameters=_obj({"ticker": _TICKER}, ["ticker"]),
        target="v2.bot.state.watchlist_remove",
        invoke_style="args",
        arg_order=("ticker",),
        mutating=True,
        cost_hint=0.1,
    ),
    ToolSpec(
        name="alert_set",
        description=(
            "Create a price alert that fires once when a ticker crosses a level. "
            "Writes to the database."
        ),
        parameters=_obj(
            {"ticker": _TICKER,
             "target_price": {"type": "number"},
             "direction": {"type": "string", "enum": ["above", "below"]}},
            ["ticker", "target_price", "direction"],
        ),
        target="v2.bot.responders.alert_set",
        invoke_style="args",
        arg_order=("ticker", "target_price", "direction"),
        mutating=True,
        cost_hint=0.2,
    ),
    ToolSpec(
        name="alert_list",
        description="List the user's active (unfired) price alerts.",
        parameters=_EMPTY,
        target="v2.bot.responders.alert_list_view",
        invoke_style="none",
        cost_hint=0.1,
    ),
    ToolSpec(
        name="alert_remove",
        description="Delete one price alert by its id. Writes to the database.",
        parameters=_obj({"alert_id": {"type": "integer"}}, ["alert_id"]),
        target="v2.bot.responders.alert_remove_view",
        invoke_style="args",
        arg_order=("alert_id",),
        mutating=True,
        cost_hint=0.1,
    ),
    ToolSpec(
        name="settings_view",
        description="Current push thresholds and bot settings.",
        parameters=_EMPTY,
        target="v2.bot.responders.settings_view",
        invoke_style="none",
        cost_hint=0.1,
    ),
)

SPECS_BY_NAME: dict[str, ToolSpec] = {s.name: s for s in TOOL_SPECS}


# ---------------------------------------------------------------------------
# Argument normalisation
# ---------------------------------------------------------------------------

def _coerce(spec: ToolSpec, args: dict[str, Any]) -> dict[str, Any]:
    """Coerce model-supplied args to the shapes the responders expect.

    Models emit ``"130"`` where the responder wants ``130.0`` and ``nvda`` where
    it wants ``NVDA``. The bot solved this in ``intent.classify`` with hand-rolled
    defensive parsing; here it belongs next to the schema that declared the type.
    """
    props: dict[str, Any] = spec.parameters.get("properties", {})
    out: dict[str, Any] = {}
    for key, value in (args or {}).items():
        if key not in props:
            continue  # drop hallucinated parameters rather than pass them through
        declared = props[key].get("type")
        try:
            if declared == "integer":
                value = int(float(value))
            elif declared == "number":
                value = float(value)
            elif declared == "string":
                value = str(value).strip()
        except (TypeError, ValueError):
            continue
        if key in ("ticker", "symbol") and isinstance(value, str):
            value = value.upper()
        if key in ("manager", "direction", "release_type", "period") and isinstance(value, str):
            value = value.lower()
        enum = props[key].get("enum")
        if enum and value not in enum:
            continue  # let the required-arg check below report it
        out[key] = value
    return out


def _stringify(value: Any) -> str:
    """Flatten whatever a responder returns into observation text.

    Responders are not uniform: most return str, ``institutional_quick`` returns
    list[str], ``moneyflow_view`` returns (text, png_bytes), ``watchlist_list``
    returns list[dict]. The agent only consumes text, so binary payloads are
    dropped here rather than leaking into the context window.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, tuple):
        return _stringify(value[0]) if value else ""
    if isinstance(value, list):
        if all(isinstance(v, str) for v in value):
            return "\n\n".join(value)
        return "\n".join(str(v) for v in value)
    if isinstance(value, bool):
        return "done" if value else "no-op (nothing matched)"
    return str(value)


# ---------------------------------------------------------------------------
# Executors
# ---------------------------------------------------------------------------

class LiveExecutor:
    """Resolves the dotted path and calls the real responder."""

    def __init__(self) -> None:
        self._cache: dict[str, Callable[..., Any]] = {}

    def resolve(self, spec: ToolSpec) -> Callable[..., Any]:
        fn = self._cache.get(spec.target)
        if fn is None:
            module_path, _, attr = spec.target.rpartition(".")
            fn = getattr(importlib.import_module(module_path), attr)
            self._cache[spec.target] = fn
        return fn

    def __call__(self, spec: ToolSpec, args: dict[str, Any]) -> str:
        fn = self.resolve(spec)
        if spec.invoke_style == "none":
            return _stringify(fn())
        if spec.invoke_style == "args":
            missing = [k for k in spec.arg_order if k not in args]
            if missing:
                raise ValueError(f"missing required argument(s): {', '.join(missing)}")
            return _stringify(fn(*[args[k] for k in spec.arg_order]))
        return _stringify(fn(args))


class FixtureExecutor:
    """Serves canned observations so a run needs no data-provider keys.

    This is the seed of the record/replay story: the same interface the live
    executor implements, backed by text captured from a real run. It lets the
    loop be demoed, tested and evaluated deterministically — an agent whose
    tools hit the network cannot be regression-tested at all.
    """

    def __init__(self, fixtures: dict[str, Any], strict: bool = False) -> None:
        self.fixtures = fixtures
        self.strict = strict
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, spec: ToolSpec, args: dict[str, Any]) -> str:
        self.calls.append((spec.name, dict(args)))
        entry = self.fixtures.get(spec.name)
        if entry is None:
            if self.strict:
                raise KeyError(f"no fixture recorded for tool '{spec.name}'")
            return f"(no fixture for {spec.name})"
        if callable(entry):
            return _stringify(entry(args))
        if isinstance(entry, dict):
            key = str(args.get("ticker") or args.get("symbol")
                      or args.get("manager") or args.get("period")
                      or args.get("release_type") or "_")
            if key not in entry and self.strict:
                raise KeyError(f"no fixture for {spec.name}[{key}]")
            return _stringify(entry.get(key, entry.get("_", f"(no fixture for {spec.name}/{key})")))
        return _stringify(entry)


class ToolRegistry:
    """The model-facing tool surface, plus the policy gate in front of it."""

    def __init__(
        self,
        executor: Callable[[ToolSpec, dict[str, Any]], str] | None = None,
        specs: tuple[ToolSpec, ...] = TOOL_SPECS,
        allow_mutations: bool = False,
        max_observation_chars: int = 4000,
    ) -> None:
        self.executor = executor or LiveExecutor()
        self.specs = specs
        self.allow_mutations = allow_mutations
        self.max_observation_chars = max_observation_chars

    def schemas(self) -> list[dict[str, Any]]:
        return [s.to_openai_schema() for s in self.specs]

    def names(self) -> list[str]:
        return [s.name for s in self.specs]

    def call(self, name: str, args: dict[str, Any] | None = None) -> ToolResult:
        """Run one tool. Never raises — every failure is returned as an observation."""
        args = args or {}
        started = time.time()

        spec = SPECS_BY_NAME.get(name)
        if spec is None or spec not in self.specs:
            near = [n for n in self.names() if name and name.lower() in n.lower()]
            hint = f" Did you mean: {', '.join(near[:3])}?" if near else ""
            return ToolResult(
                name=name, args=args, ok=False,
                content=f"unknown tool '{name}'.{hint} Valid tools: {', '.join(self.names())}",
                error_kind="unknown_tool",
            )

        if spec.mutating and not self.allow_mutations:
            return ToolResult(
                name=name, args=args, ok=False,
                content=(f"'{name}' writes to the user's database and mutations are "
                         "disabled for this run. Report what you would have changed "
                         "instead of retrying."),
                error_kind="mutation_blocked",
            )

        clean = _coerce(spec, args)
        required = spec.parameters.get("required", [])
        missing = [k for k in required if k not in clean]
        if missing:
            return ToolResult(
                name=name, args=args, ok=False,
                content=(f"missing or invalid required argument(s): {', '.join(missing)}. "
                         f"Schema: {spec.parameters}"),
                error_kind="bad_arguments",
            )

        try:
            content = self.executor(spec, clean)
        except Exception as exc:  # noqa: BLE001 — the model is the error handler here
            return ToolResult(
                name=name, args=clean, ok=False,
                content=f"{type(exc).__name__}: {exc}",
                elapsed_ms=int((time.time() - started) * 1000),
                error_kind=type(exc).__name__,
            )

        content = content or "(empty result)"
        truncated = False
        if len(content) > self.max_observation_chars:
            content = content[: self.max_observation_chars] + "\n…[truncated]"
            truncated = True

        return ToolResult(
            name=name, args=clean, ok=True, content=content,
            elapsed_ms=int((time.time() - started) * 1000),
            meta={"truncated": truncated, "mutating": spec.mutating},
        )
