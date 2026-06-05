"""Synchronous helpers behind the Stage 2 action commands.

Each function does the heavy lifting (FD + LLM + formatting) and returns a
ready-to-send HTML string. The bot's async command handlers call these via
loop.run_in_executor() so the bot stays responsive during long requests.

By design, every responder is self-contained (own FDClient context, own
memory init, own formatting) — that way a misbehaving /why call can't poison
the next /summary.
"""

from __future__ import annotations

import html
import logging
from datetime import date, timedelta

import numpy as np

from v2.data import CachedFDClient
from v2.data_safety import fd_safe_today
from v2.institutional import MANAGERS
from v2.observability import emit
from v2.institutional.client import fetch_recent_13f
from v2.institutional.detector import detect_changes
from v2.institutional.models import InstitutionalReport
from v2.institutional.summarizer import interpret_changes
from v2.lateral import LATERAL_FILTERS, run_lateral_expansion
from v2.memory import AnomalyMemory
from v2.monitoring import attribute
from v2.monitoring.models import Anomaly, MonitorConfig
from v2.reporting import (
    format_alert_list,
    format_anomaly_alert,
    format_etf_snapshot,
    format_holders,
    format_institutional_messages,
    format_lateral_result,
    format_pnl,
    format_portfolio,
    format_portfolio_snapshot,
)
from v2.screening import (
    DEFAULT_FILTERS,
    TECH_30,
    build_candidate,
)
from v2.screening.delta_fetcher import fetch_news_headlines
from v2.screening.screener import enrich_with_earnings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# /why TICKER — explain recent move
# ---------------------------------------------------------------------------


def explain_move(ticker: str) -> str:
    """Build an on-demand Anomaly for *ticker* and run the full attribution chain."""
    ticker = ticker.upper()
    try:
        with CachedFDClient() as fd:
            anomaly = _build_query_anomaly(ticker, fd)
            if anomaly is None:
                return (
                    f"<b>🚫 No price data for {html.escape(ticker)}</b>\n"
                    "Check the ticker symbol and try again."
                )
            try:
                memory = AnomalyMemory()
            except Exception:
                memory = None
            attribute(anomaly, fd_client=fd, memory=memory)
    except Exception as exc:
        logger.exception("explain_move failed for %s", ticker)
        return f"❌ Error: <code>{html.escape(str(exc))}</code>"

    return format_anomaly_alert(anomaly)


def _build_query_anomaly(ticker: str, fd: CachedFDClient) -> Anomaly | None:
    """Construct an Anomaly representing 'user asked about this ticker today'."""
    # fd_safe_today caps end_date at today - 3 days so we don't request
    # past FD's coverage window (which would return HTTP 400 → empty).
    today = fd_safe_today()
    start = (today - timedelta(days=400)).isoformat()
    prices = fd.get_prices(ticker, start, today.isoformat())
    if not prices or len(prices) < 2:
        return None

    closes = np.array([p.close for p in prices], dtype=float)
    vols = np.array([p.volume for p in prices], dtype=float)
    latest = prices[-1]
    prev = prices[-2]
    avg30 = float(vols[-31:-1].mean()) if len(vols) >= 31 else float(vols[-len(vols) // 2:].mean())

    return Anomaly(
        ticker=ticker,
        date=latest.time[:10],
        price=float(latest.close),
        price_change_pct=float((latest.close - prev.close) / prev.close) if prev.close > 0 else 0.0,
        volume_today=int(latest.volume),
        volume_avg_30d=avg30,
        volume_ratio=float(latest.volume / avg30) if avg30 > 0 else 1.0,
        high_52w=float(closes[-252:].max()) if len(closes) >= 252 else float(closes.max()),
        low_52w=float(closes[-252:].min()) if len(closes) >= 252 else float(closes.min()),
        flags=[],  # user query — no detector fired
        recent_prices=[float(p.close) for p in prices[-7:]],
    )


# ---------------------------------------------------------------------------
# /summary TICKER — multi-section overview
# ---------------------------------------------------------------------------


def summary(ticker: str) -> str:
    ticker = ticker.upper()
    # fd_safe_today: stay inside FD's coverage window — see v2/data_safety.py.
    today = fd_safe_today()
    history_start = (today - timedelta(days=400)).isoformat()
    today_str = today.isoformat()

    try:
        with CachedFDClient() as fd:
            candidate = build_candidate(ticker, fd, today_str, history_start)
            if candidate is None:
                return f"<b>🚫 No data for {html.escape(ticker)}</b>"

            enrich_with_earnings(candidate, fd)

            # Insider activity (latest 30 days)
            insider_lines = _insider_snippet(ticker, fd, today_str)

            # Recent news (7 days, top 3)
            headlines = fetch_news_headlines(ticker, max_results=3)
    except Exception as exc:
        logger.exception("summary failed for %s", ticker)
        return f"❌ Error: <code>{html.escape(str(exc))}</code>"

    lines: list[str] = [
        f"<b>📊 {html.escape(ticker)} · Summary · {today_str}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"价格 <code>${candidate.price:,.2f}</code>"
        f"  {_fmt_change(candidate.price_change)}",
    ]
    if candidate.return_1w is not None:
        lines.append(f"周回报 <code>{candidate.return_1w:+.1%}</code>")

    # Fundamentals
    lines.append("")
    lines.append("<b>📈 基本面</b>")
    lines.append(
        f"   市值 <code>${_short_money(candidate.market_cap)}</code> · "
        f"毛利 <code>{_pct1(candidate.gross_margin)}</code> · "
        f"营收 <code>{_pct1(candidate.revenue_growth, signed=True)}</code> · "
        f"波动 <code>{_pct1(candidate.volatility)}</code>"
    )

    # Earnings surprise
    if candidate.revenue_surprise_pct is not None:
        emoji = "🟢" if candidate.revenue_surprise_pct >= 0.02 else ("🔴" if candidate.revenue_surprise_pct <= -0.02 else "🟡")
        lines.append("")
        lines.append("<b>💰 最近一季</b>")
        lines.append(
            f"   营收实际 <code>${_short_money(candidate.revenue_actual or 0)}</code>"
            f" / 预期 <code>${_short_money(candidate.revenue_estimate or 0)}</code>"
            f"  {emoji} <code>{candidate.revenue_surprise_pct:+.1%}</code>"
        )
        if candidate.eps_actual is not None:
            eps_emoji = "🟢" if (candidate.eps_surprise_pct or 0) >= 0.02 else ("🔴" if (candidate.eps_surprise_pct or 0) <= -0.02 else "🟡")
            lines.append(
                f"   EPS 实际 <code>${candidate.eps_actual:.2f}</code>"
                f" / 预期 <code>${candidate.eps_estimate or 0:.2f}</code>"
                f"  {eps_emoji} <code>{(candidate.eps_surprise_pct or 0):+.1%}</code>"
            )

    # Insider
    if insider_lines:
        lines.append("")
        lines.append("<b>👥 内部人活动（近 30 日）</b>")
        lines.extend(insider_lines)

    # News
    if headlines:
        lines.append("")
        lines.append("<b>📰 近期新闻（7 天）</b>")
        for h in headlines[:3]:
            title = html.escape((h.get("title") or "")[:90])
            lines.append(f"   • {title}")

    emit("render", card="summary_card",
         ticker=ticker, num_news=len(headlines or []))
    return "\n".join(lines)


def _insider_snippet(ticker: str, fd, asof: str) -> list[str]:
    """Compact summary of the last 30 days of insider trades."""
    try:
        from v2.monitoring.detectors import _detect_insider_activity
        info = _detect_insider_activity(ticker, fd, asof, MonitorConfig())
    except Exception:
        return []
    if info is None:
        return ["   <i>无显著开放市场交易</i>"]

    verb = "净买入" if info.net_value > 0 else "净卖出"
    lines = [
        f"   {verb} <code>${_short_money(abs(info.net_value))}</code> · "
        f"{info.trade_count} 笔"
    ]
    for ex in info.executives[:2]:
        arrow = "买入" if ex.direction == "buy" else "卖出"
        lines.append(
            f"   {html.escape(ex.title[:24])} {html.escape(ex.name[:20])} "
            f"{arrow} <code>${_short_money(ex.value)}</code>"
        )
    return lines


# ---------------------------------------------------------------------------
# /chain TICKER — lateral expansion for one seed
# ---------------------------------------------------------------------------


def chain(ticker: str) -> str:
    ticker = ticker.upper()
    universe = set(TECH_30)
    try:
        with CachedFDClient() as fd:
            result = run_lateral_expansion(
                seeds=[ticker],
                universe=universe,
                fd_client=fd,
                filter_config=LATERAL_FILTERS,
            )
    except Exception as exc:
        logger.exception("chain failed for %s", ticker)
        return f"❌ Error: <code>{html.escape(str(exc))}</code>"

    return format_lateral_result(result)


# ---------------------------------------------------------------------------
# /13f MANAGER — single-manager institutional report
# ---------------------------------------------------------------------------


_MANAGER_ALIASES = {
    "brk":         ("1067983", "Berkshire Hathaway"),
    "berkshire":   ("1067983", "Berkshire Hathaway"),
    "buffett":     ("1067983", "Berkshire Hathaway"),
    "burry":       ("1649339", "Scion Asset Mgmt"),
    "scion":       ("1649339", "Scion Asset Mgmt"),
    "ackman":      ("1336528", "Pershing Square Capital"),
    "pershing":    ("1336528", "Pershing Square Capital"),
    "einhorn":     ("1079114", "Greenlight Capital"),
    "greenlight":  ("1079114", "Greenlight Capital"),
    "renaissance": ("1037389", "Renaissance Technologies"),
    "rentech":     ("1037389", "Renaissance Technologies"),
    "twosigma":    ("1179392", "Two Sigma Investments"),
    "deshaw":      ("1009207", "D.E. Shaw & Co"),
    "shaw":        ("1009207", "D.E. Shaw & Co"),
    "citadel":     ("1423053", "Citadel Advisors"),
    "coatue":      ("1135730", "Coatue Management"),
    "ark":         ("1697748", "ARK Investment Mgmt"),
    "cathie":      ("1697748", "ARK Investment Mgmt"),
    "wood":        ("1697748", "ARK Investment Mgmt"),
}


def institutional_quick(name_input: str) -> list[str]:
    """Always-fresh 13F view for the bot.

    Unlike the scheduled agent (which suppresses 'already-seen' filings to
    avoid duplicate pushes), the bot's /13f command should ALWAYS show the
    manager's latest known holdings + QoQ changes. Conversational UX wins:
    "show me ARK's latest" → show it, regardless of DB state.

    No edgar.db mutation here — read-only on every call.
    """
    key = name_input.strip().lower()
    target = _MANAGER_ALIASES.get(key)
    if target is None:
        valid = ", ".join(sorted({n for n in _MANAGER_ALIASES if len(n) >= 4}))
        return [(
            f"<b>🚫 Unknown manager: {html.escape(name_input)}</b>\n"
            f"支持的别名：<code>{html.escape(valid)}</code>"
        )]

    cik, full_name = target

    try:
        recent = fetch_recent_13f(cik, full_name, n_filings=2)
    except Exception as exc:
        logger.exception("/13f EDGAR fetch failed for %s", full_name)
        return [f"❌ EDGAR error: <code>{html.escape(str(exc))}</code>"]

    if not recent:
        return [(
            f"<b>🏛️ {html.escape(full_name)}</b>\n"
            "<i>EDGAR 没有可读取的 13F-HR 文件</i>"
        )]

    current_filing, current_positions = recent[0]

    if len(recent) < 2:
        # Single filing — show it but no QoQ comparison available
        report = InstitutionalReport(
            date=date.today().isoformat(),
            new_filings=[current_filing],
            changes=[],
            api_calls=1,
            llm_tokens=0,
        )
        return format_institutional_messages(report)

    prev_filing, prev_positions = recent[1]

    cur_dicts = [_pos_to_dict(p) for p in current_positions]
    prev_dicts = [_pos_to_dict(p) for p in prev_positions]

    changes = detect_changes(
        cik=cik,
        manager_name=full_name,
        quarter=current_filing.quarter,
        current_positions=cur_dicts,
        prev_positions=prev_dicts,
        current_total=current_filing.portfolio_value,
        prev_total=prev_filing.portfolio_value,
    )

    # Flag tickers already in our monitored universe
    universe = set(TECH_30)
    for c in changes:
        if c.ticker and c.ticker in universe:
            c.in_universe = True

    # LLM interpretation (cap at top 20 to control tokens)
    llm_tokens = 0
    if changes:
        try:
            interpretations, llm_tokens = interpret_changes(full_name, changes[:20])
            for c in changes[:20]:
                ck = c.ticker or c.cusip
                if ck in interpretations:
                    c.interpretation = interpretations[ck]
        except Exception as exc:
            logger.warning("interpret_changes failed for %s: %s", full_name, exc)

    report = InstitutionalReport(
        date=date.today().isoformat(),
        new_filings=[current_filing],
        changes=changes,
        api_calls=1,
        llm_tokens=llm_tokens,
    )

    # NEW: full-portfolio snapshot card BEFORE the changes card.
    # Conversational priority: "what does ARK hold right now?" comes first;
    # "what changed last quarter?" is the next page.
    snapshot = format_portfolio_snapshot(
        current_filing, current_positions, top_n=10,
    )
    return [snapshot] + format_institutional_messages(report)


# ---------------------------------------------------------------------------
# /holders TICKER — reverse query: which tracked managers hold this ticker?
# ---------------------------------------------------------------------------


def holders(ticker_input: str) -> str:
    """Cross-manager holdings for a single ticker, served from edgar.db.

    Reads only — never hits EDGAR. As-of latest filing already in DB per
    manager. Sub-second.
    """
    from v2.institutional.tracker import get_db

    ticker = ticker_input.strip().upper()
    emit("validate", what="ticker", input=ticker_input[:40], passed=bool(ticker) and ticker.isalpha())
    if not ticker or not ticker.isalpha():
        return f"<b>🚫 Invalid ticker: {html.escape(ticker_input)}</b>"

    held: list[dict] = []
    not_held: list[str] = []
    unknown: list[str] = []

    try:
        emit("db_read", db="edgar.db", table="filings+positions",
             where=f"cross-manager lookup for {ticker}")
        with get_db() as conn:
            for cik, manager_name in MANAGERS:
                row = conn.execute(
                    """SELECT accession, quarter, portfolio_value
                       FROM filings WHERE cik=?
                       ORDER BY period_of_report DESC LIMIT 1""",
                    (cik,),
                ).fetchone()
                if not row:
                    unknown.append(manager_name)
                    continue

                pos = conn.execute(
                    """SELECT shares, market_value, issuer_name
                       FROM positions
                       WHERE accession=? AND ticker=?""",
                    (row["accession"], ticker),
                ).fetchone()

                if pos is None:
                    not_held.append(manager_name)
                    continue

                port_v = row["portfolio_value"] or 1
                held.append({
                    "manager": manager_name,
                    "shares":  pos["shares"],
                    "value":   pos["market_value"],
                    "pct":     pos["market_value"] / port_v,
                    "quarter": row["quarter"],
                })
    except Exception as exc:
        logger.exception("holders failed for %s", ticker)
        return f"❌ DB error: <code>{html.escape(str(exc))}</code>"

    held.sort(key=lambda h: h["value"], reverse=True)
    emit("render", card="holders_card",
         ticker=ticker, num_held=len(held), num_not_held=len(not_held))
    return format_holders(ticker, held, not_held, unknown)


# ---------------------------------------------------------------------------
# /etf SYMBOL — ARK fund daily holdings
# ---------------------------------------------------------------------------


def etf_view(symbol_input: str) -> str:
    """Latest holdings + 24h changes for an ARK fund."""
    from v2.etf import (
        SUPPORTED_FUNDS,
        compute_daily_changes,
        fetch_holdings,
        get_latest_snapshot_before,
        save_snapshot,
    )

    symbol = symbol_input.strip().upper()
    emit("validate", what="ticker", input=symbol_input[:40],
         passed=symbol in SUPPORTED_FUNDS, allowed=list(SUPPORTED_FUNDS))
    if symbol not in SUPPORTED_FUNDS:
        return (
            f"<b>🚫 Unsupported ETF: {html.escape(symbol_input)}</b>\n"
            f"支持：<code>{html.escape(', '.join(SUPPORTED_FUNDS))}</code>"
        )

    try:
        holdings, snapshot_date = fetch_holdings(symbol)
    except Exception as exc:
        logger.exception("/etf fetch failed for %s", symbol)
        return f"❌ Fetch error: <code>{html.escape(str(exc))}</code>"

    if not holdings:
        return (
            f"<b>📈 {html.escape(symbol)}</b>\n"
            "<i>未能获取 CSV 数据（issuer 可能临时不可用）</i>"
        )

    # Persist + diff against the prior snapshot (different date) if available
    daily_changes = None
    try:
        emit("db_read", db="etf.db", table="etf_snapshots",
             where=f"latest prior snapshot for {symbol} before {snapshot_date}")
        prev = get_latest_snapshot_before(symbol, snapshot_date)
        if prev:
            daily_changes = compute_daily_changes(prev, holdings)
        save_snapshot(symbol, snapshot_date, holdings)
    except Exception as exc:
        logger.warning("ETF persistence failed for %s: %s", symbol, exc)

    emit("render", card="etf_snapshot",
         etf=symbol, snapshot_date=snapshot_date,
         positions=len(holdings),
         changes=len(daily_changes) if daily_changes else 0)
    return format_etf_snapshot(
        symbol, holdings, snapshot_date,
        top_n=15, daily_changes=daily_changes,
    )


def _pos_to_dict(p) -> dict:
    """Pydantic Position → dict shape detect_changes() expects."""
    if isinstance(p, dict):
        return p
    return {
        "cusip": p.cusip,
        "ticker": p.ticker,
        "issuer_name": p.issuer_name,
        "shares": p.shares,
        "market_value": p.market_value,
    }


# ---------------------------------------------------------------------------
# /settings — read-only view of current thresholds
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# /alert TICKER PRICE [above|below] — set price alert
# ---------------------------------------------------------------------------


def alert_set(ticker: str, target_price: float, direction: str = "above") -> str:
    """Create one price alert and return a confirmation card."""
    from v2.bot import state
    emit("validate", what="ticker", input=ticker[:40], passed=True)
    emit("validate", what="price",
         price=float(target_price), direction=direction,
         passed=target_price > 0 and direction in ("above", "below"))
    try:
        alert_id = state.alert_add(ticker, direction, target_price)
    except ValueError as exc:
        return f"<b>🚫 无效输入：</b> {html.escape(str(exc))}"
    sign = "≥" if direction == "above" else "≤"
    return (
        f"<b>🔔 已设置提醒</b> <code>#{alert_id}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>{html.escape(ticker.upper())}</b> {sign} "
        f"<code>${target_price:,.2f}</code>\n\n"
        f"<i>下次 streamer 轮询到该价位时立刻推送。</i>"
    )


def alert_list_view() -> str:
    """Return the user's open alerts as a Telegram card."""
    from v2.bot import state
    emit("db_read", db="bot_state.db", table="alerts",
         where="fired_at IS NULL")
    alerts = state.alert_list(include_fired=False)
    emit("render", card="alerts_list", num_alerts=len(alerts))
    return format_alert_list(alerts)


def alert_remove_view(alert_id: int) -> str:
    from v2.bot import state
    removed = state.alert_remove(alert_id)
    if not removed:
        return f"<i>提醒 <code>#{alert_id}</code> 不存在或已被删除。</i>"
    return f"<b>🗑 已删除提醒</b> <code>#{alert_id}</code>"


# ---------------------------------------------------------------------------
# /portfolio /pnl — Alpaca account snapshot
# ---------------------------------------------------------------------------


def portfolio_view() -> str:
    """Return the Alpaca portfolio card."""
    from v2.broker import AlpacaUnavailable, get_portfolio
    try:
        snap = get_portfolio()
    except AlpacaUnavailable as exc:
        return f"<b>⚠️ Alpaca 不可用</b>\n<i>{html.escape(str(exc))}</i>"
    except Exception as exc:
        logger.exception("/portfolio failed")
        return f"❌ Error: <code>{html.escape(str(exc))}</code>"
    emit("render", card="portfolio_card",
         positions=len(snap.get("positions") or []))
    return format_portfolio(snap)


def pnl_view() -> str:
    """Return the Alpaca P&L card."""
    from v2.broker import AlpacaUnavailable, get_pnl
    try:
        snap = get_pnl()
    except AlpacaUnavailable as exc:
        return f"<b>⚠️ Alpaca 不可用</b>\n<i>{html.escape(str(exc))}</i>"
    except Exception as exc:
        logger.exception("/pnl failed")
        return f"❌ Error: <code>{html.escape(str(exc))}</code>"
    emit("render", card="pnl_card",
         positions=len(snap.get("positions") or []))
    return format_pnl(snap)


# ---------------------------------------------------------------------------
# /risk — portfolio risk snapshot (Phase 2 Stage 4, read-only)
# ---------------------------------------------------------------------------


_VALID_PNL_PERIODS = frozenset({"day", "week", "month"})


def risk_view(args: dict) -> str:
    """Real-time portfolio risk card.

    args: ``{}`` — no parameters. Calls :func:`build_risk_report` and
    renders via :func:`v2.reporting.format_portfolio_risk_view` (Stage 5
    lift — byte-equal alias of ``format_portfolio_risk_card`` so the bot
    card matches what ⑨ pushes). Read-only — no archive write, no
    priority computed (priority is a cron-push concept).
    """
    from v2.portfolio import build_risk_report
    from v2.reporting import format_portfolio_risk_view

    try:
        report = build_risk_report()
    except Exception as exc:
        logger.exception("risk_view failed")
        return f"❌ Error: <code>{html.escape(str(exc))}</code>"

    emit(
        "render", card="risk_card",
        positions=len(report.positions),
        warnings=len(report.warnings),
    )
    return format_portfolio_risk_view(report)


def pnl_period(args: dict) -> str:
    """Period-specific P&L card.

    args: ``{"period": "day" | "week" | "month"}`` (default ``"day"``).
    Invalid period strings return a friendly error rather than silently
    defaulting — the user typed something specific, they should see it
    was rejected.

    day path reuses ``format_pnl`` (the pre-existing daily formatter,
    matches /pnl no-arg byte-equal). week/month use the Stage-5 lift
    ``format_portfolio_pnl_period``.
    """
    period = str(args.get("period") or "day").strip().lower()
    if period not in _VALID_PNL_PERIODS:
        return (
            f"<b>🚫 未知周期：</b> <code>{html.escape(period)}</code>\n"
            "可选：<code>day</code> / <code>week</code> / <code>month</code>"
        )

    from v2.broker import AlpacaUnavailable, get_pnl
    from v2.portfolio.pnl import compute_pnl
    from v2.reporting import format_portfolio_pnl_period

    if period == "day":
        try:
            snap = get_pnl()
        except AlpacaUnavailable as exc:
            return f"<b>⚠️ Alpaca 不可用</b>\n<i>{html.escape(str(exc))}</i>"
        except Exception as exc:
            logger.exception("/pnl day failed")
            return f"❌ Error: <code>{html.escape(str(exc))}</code>"
        emit("render", card="pnl_card",
             positions=len(snap.get("positions") or []))
        return format_pnl(snap)

    try:
        metrics, _warnings = compute_pnl()
    except Exception as exc:
        logger.exception("pnl_period(%s) failed", period)
        return f"❌ Error: <code>{html.escape(str(exc))}</code>"

    emit("render", card="pnl_period_card", period=period)
    return format_portfolio_pnl_period(period, metrics)


def settings_view() -> str:
    monitor = MonitorConfig()
    screen = DEFAULT_FILTERS
    lateral = LATERAL_FILTERS
    emit("render", card="settings_card")

    return "\n".join([
        "<b>⚙️ Settings (read-only)</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        "<b>① Screening filter (玩法 ①)</b>",
        f"   市值 <code>${_short_money(screen.market_cap_min)}</code>"
        f" – <code>${_short_money(screen.market_cap_max)}</code>",
        f"   营收增速 ≥ <code>{screen.revenue_growth_min:.1%}</code>",
        f"   毛利率 ≥ <code>{screen.gross_margin_min:.1%}</code>",
        f"   年化波动 ≤ <code>{screen.volatility_max:.1%}</code>",
        "",
        "<b>② Anomaly thresholds (玩法 ②)</b>",
        f"   成交量倍数 ≥ <code>{monitor.volume_spike_threshold:.1f}x</code>",
        f"   52w 高 / 低 容忍度 <code>{(1 - monitor.high_52w_threshold):.1%}</code>",
        f"   内部人 net 买入 ≥ <code>${_short_money(monitor.insider_buy_min_value)}</code>",
        f"   内部人 net 卖出 ≥ <code>${_short_money(monitor.insider_sell_min_value)}</code>",
        "",
        "<b>③ Lateral filter (玩法 ③)</b>",
        f"   市值 ≥ <code>${_short_money(lateral.market_cap_min)}</code>",
        f"   营收增速 ≥ <code>{lateral.revenue_growth_min:.1%}</code>",
        f"   毛利率 ≥ <code>{lateral.gross_margin_min:.1%}</code>",
        "",
        "<i>编辑功能将在后续版本上线。</i>",
    ])


# ---------------------------------------------------------------------------
# /earnings — single-ticker card + N-day calendar (Phase 1 Stage 4)
# ---------------------------------------------------------------------------


_DEFAULT_HORIZON_DAYS = 14


def earnings_view(args: dict) -> str:
    """Single-ticker earnings card.

    args: ``{"ticker": "AAPL"}``

    Composes the next yfinance calendar entry + last FD filing into one
    HTML card. Read-only — no archive write, no priority scoring.
    Failures degrade to a friendly message; never raises.
    """
    from v2.earnings import (
        get_latest_actual,
        get_upcoming,
        is_supported_ticker,
    )
    from v2.earnings._bot_cards import format_earnings_view

    ticker = str(args.get("ticker") or "").strip().upper()
    if not ticker or not is_supported_ticker(ticker):
        return (
            f"<b>🚫 无效 ticker: {html.escape(ticker or '(empty)')}</b>\n"
            "请使用美股 ticker（如 <code>AAPL</code>、<code>BRK.B</code>）"
        )

    next_event = None
    try:
        next_event = get_upcoming(ticker)
    except Exception as exc:
        logger.warning("earnings_view: get_upcoming(%s) failed: %s", ticker, exc)

    last_event = None
    try:
        with CachedFDClient() as fd:
            last_event = get_latest_actual(fd, ticker)
    except Exception as exc:
        logger.warning("earnings_view: get_latest_actual(%s) failed: %s", ticker, exc)

    if next_event is None and last_event is None:
        return f"<i>暂未取到 {html.escape(ticker)} 财报数据</i>"

    is_held, is_watchlist = _ticker_membership(ticker)
    return format_earnings_view(
        ticker,
        next_event=next_event,
        last_event=last_event,
        is_held=is_held,
        is_watchlist=is_watchlist,
    )


def earnings_calendar(args: dict) -> str:
    """N-day forward calendar across (watchlist ∪ Alpaca holdings).

    args: ``{"days_horizon": 14}`` (default 14)

    Returns one card listing every release within the horizon, sorted by
    date, with ⭐ chips. Empty universe / empty horizon both produce a
    polite "no upcoming" message.
    """
    from v2.earnings import get_upcoming_batch
    from v2.earnings._bot_cards import format_earnings_calendar

    horizon = args.get("days_horizon")
    try:
        horizon = int(horizon) if horizon else _DEFAULT_HORIZON_DAYS
    except (TypeError, ValueError):
        horizon = _DEFAULT_HORIZON_DAYS
    horizon = max(1, min(horizon, 90))  # bound to a sane range

    held, watchlist = _user_universe()
    universe = sorted(held | watchlist)
    if not universe:
        return (
            f"<b>📅 未来 {horizon} 天财报日历</b>\n"
            "<i>watchlist 和持仓 都为空，先 /add TICKER 添加几只</i>"
        )

    try:
        batch = get_upcoming_batch(universe)
    except Exception as exc:
        logger.warning("earnings_calendar: get_upcoming_batch failed: %s", exc)
        return f"❌ 日历查询失败: <code>{html.escape(str(exc))}</code>"

    return format_earnings_calendar(
        batch.events.values(),
        horizon_days=horizon,
        held=held,
        watchlist=watchlist,
    )


# ---------------------------------------------------------------------------
# /8k + /insiders — SEC monitoring on-demand queries (Phase 3 Stage 4)
# ---------------------------------------------------------------------------
# Read-only contract — no archive write, no priority computed.
# Same as Phase 1 earnings_view and Phase 2 risk_view, mirrors that
# semantic.

_DEFAULT_8K_DAYS = 30
_DEFAULT_INSIDER_DAYS_BACK = 90
_INSIDER_MIN_DAYS = 7
_INSIDER_MAX_DAYS = 365


def _is_valid_ticker(s: str) -> bool:
    """US ticker shape: 1-5 uppercase letters (Berkshire-style BRK.A
    not supported by this view but rare in queryable universe)."""
    return bool(s) and 1 <= len(s) <= 5 and s.isascii() and s.isalpha()


def eight_k_view(args: dict) -> str:
    """Single-ticker 8-K history card (last 30 days).

    args: ``{"ticker": "AAPL"}``

    For each filing in the window, parses items + runs 5.02 LLM
    extraction (reuses the cron path). On any LLM failure shows
    "(姓名待解析)" placeholder instead of crashing the card.

    Read-only — never writes archive, never computes priority. Bot
    surface, not push surface.
    """
    from datetime import date, timedelta

    from v2.sec import client as sec_client
    from v2.sec import eight_k_parser, ner_5_02
    from v2.sec.models import SecFiling

    ticker = str(args.get("ticker") or "").strip().upper()
    if not _is_valid_ticker(ticker):
        return (
            f"<b>🚫 无效 ticker: {html.escape(ticker or '(empty)')}</b>\n"
            "请使用美股 ticker（1-5 大写字母，如 <code>AAPL</code>）"
        )

    today = date.today()
    since = (today - timedelta(days=_DEFAULT_8K_DAYS)).isoformat()
    until = today.isoformat()

    try:
        filings = sec_client.get_recent_filings(ticker, "8-K", since, until)
    except Exception as exc:
        logger.exception("eight_k_view: SEC fetch failed for %s", ticker)
        return f"❌ SEC 查询失败: <code>{html.escape(str(exc))}</code>"

    membership_note = _build_membership_note(ticker)

    if not filings:
        return _format_8k_empty_card(ticker, _DEFAULT_8K_DAYS, membership_note)

    # Parse each filing's items + run 5.02 LLM extraction
    rendered_filings: list[dict] = []
    total_items = 0
    for f in filings:
        sec_filing = _build_sec_filing_for_view(f, ticker)
        if sec_filing is None:
            continue
        event = eight_k_parser.parse_eight_k_filing(f, sec_filing)
        if event is None:
            continue

        # Run 5.02 LLM extraction if 5.02 present + escalate tier when
        # senior_exec confirmed (mirrors cron pipeline behavior so the
        # bot card's tier chip shows P0 for CEO/CFO departures).
        extracted_5_02: dict = {}
        for idx, it in enumerate(event.items):
            if it.code != "5.02":
                continue
            try:
                obj = f.obj()
                text = eight_k_parser.get_item_text(obj, "5.02")
                extracted_5_02 = ner_5_02.extract_5_02(text)
            except Exception as exc:
                logger.warning("5.02 extraction failed in /8k %s: %s", ticker, exc)
                extracted_5_02 = {}     # → "(姓名待解析)" placeholder
            if extracted_5_02 and extracted_5_02.get("has_senior_exec"):
                event.items[idx] = it.__class__(
                    code=it.code, priority_tier="P0",
                    description=it.description,
                    extracted_meta=extracted_5_02,
                )
            break

        rendered_filings.append({
            "filing": event.filing,
            "items": event.items,
            "extracted_5_02": extracted_5_02,
        })
        total_items += len(event.items)

    emit("render", card="sec_8k_view_card",
         ticker=ticker, n_filings=len(rendered_filings), n_items=total_items)

    return _format_8k_view_card(
        ticker, rendered_filings, total_items, _DEFAULT_8K_DAYS, membership_note,
    )


def insider_view(args: dict) -> str:
    """Single-ticker Form 4 summary card (last N days, default 90).

    args: ``{"ticker": "NVDA", "days_back": 90}`` (days_back optional)

    Splits transactions into:
    - P (Purchase) — listed with name + role + USD
    - S (Sale) — same, marked 10b5-1 plan if applicable
    - A/M/F/G/C/D — aggregated counts only (noise codes per Stage 0)
    - Same-period clusters (≥3 distinct insiders same-day same-direction)

    No LLM calls — pure Python aggregation.
    """
    from datetime import date, timedelta

    from v2.sec import client as sec_client, cluster, form4_parser
    from v2.sec.models import (
        NOISE_TRANSACTION_CODES, SIGNAL_TRANSACTION_CODES,
    )

    ticker = str(args.get("ticker") or "").strip().upper()
    if not _is_valid_ticker(ticker):
        return (
            f"<b>🚫 无效 ticker: {html.escape(ticker or '(empty)')}</b>\n"
            "请使用美股 ticker（1-5 大写字母，如 <code>NVDA</code>）"
        )

    # days_back: int, bounded
    try:
        days_back = int(args.get("days_back") or _DEFAULT_INSIDER_DAYS_BACK)
    except (TypeError, ValueError):
        days_back = _DEFAULT_INSIDER_DAYS_BACK
    days_back = max(_INSIDER_MIN_DAYS, min(_INSIDER_MAX_DAYS, days_back))

    today = date.today()
    since = (today - timedelta(days=days_back)).isoformat()
    until = today.isoformat()

    try:
        filings = sec_client.get_recent_filings(ticker, "4", since, until)
    except Exception as exc:
        logger.exception("insider_view: SEC fetch failed for %s", ticker)
        return f"❌ SEC 查询失败: <code>{html.escape(str(exc))}</code>"

    membership_note = _build_membership_note(ticker)

    if not filings:
        return _format_insider_empty_card(ticker, days_back, membership_note)

    # Parse every Form 4 to a flat transactions list
    all_txs: list = []
    for f in filings:
        sec_filing = _build_sec_filing_for_view(f, ticker, form="4")
        if sec_filing is None:
            continue
        try:
            txs = form4_parser.parse_form4_filing(f, sec_filing)
        except Exception as exc:
            logger.warning("form4 parse failed for %s acc=%s: %s",
                           ticker, sec_filing.accession_number, exc)
            continue
        all_txs.extend(txs)

    # Bucket by code class
    purchases = [t for t in all_txs if t.transaction_code == "P"]
    sales = [t for t in all_txs if t.transaction_code == "S"]
    noise_counts: dict[str, int] = {}
    for t in all_txs:
        if t.transaction_code in NOISE_TRANSACTION_CODES:
            noise_counts[t.transaction_code] = noise_counts.get(t.transaction_code, 0) + 1

    # Same-day clusters across the whole window
    cluster_list = cluster.find_clusters(purchases + sales)

    emit("render", card="sec_insider_view_card",
         ticker=ticker, days_back=days_back,
         n_purchases=len(purchases), n_sales=len(sales),
         n_noise_codes=sum(noise_counts.values()),
         n_clusters=len(cluster_list))

    return _format_insider_view_card(
        ticker, days_back,
        purchases=purchases, sales=sales,
        noise_counts=noise_counts,
        clusters=cluster_list,
        membership_note=membership_note,
    )


# ---- /8k + /insiders helpers ----------------------------------------------


def _build_sec_filing_for_view(edgar_filing, ticker: str, *, form: str = "8-K"):
    """Mirror of pipeline._build_sec_filing but used for bot view path.

    Bot view doesn't need to track is_amendment for priority (no
    priority computed) but the SecFiling dataclass requires it.
    """
    from v2.sec.models import SecFiling
    try:
        accession = str(
            getattr(edgar_filing, "accession_number", None)
            or getattr(edgar_filing, "accession_no", None)
            or ""
        ).strip()
        filing_date = str(getattr(edgar_filing, "filing_date", "") or "").strip()
        cik = str(getattr(edgar_filing, "cik", "") or "").strip()
        actual_form = str(getattr(edgar_filing, "form", form) or form).strip()
    except Exception:
        return None

    if not accession:
        return None

    return SecFiling(
        ticker=ticker, cik=cik, form=actual_form,
        filing_date=filing_date, accession_number=accession,
        is_amendment=actual_form.endswith("/A"),
    )


def _build_membership_note(ticker: str) -> str:
    """Return small ℹ️ note if ticker is outside the user's universe.

    Read-only path lets the user query ANY ticker — even ones not in
    watchlist or holdings. The note helps the user understand context
    without blocking the query.
    """
    try:
        held, watchlist = _user_universe()
    except Exception:
        return ""
    if ticker in held or ticker in watchlist:
        return ""
    return f"<i>ℹ️ {html.escape(ticker)} 不在你的 universe (持仓 + 关注)</i>"


_TIER_EMOJI = {"P0": "🚨", "P1": "📋", "P2": "📎", "P3": "📌"}


def _format_8k_empty_card(ticker: str, days: int, membership_note: str) -> str:
    lines = [
        f"<b>📋 SEC 8-K · {html.escape(ticker)} · 过去 {days} 天</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        "<i>无 8-K 申报</i>",
    ]
    if membership_note:
        lines.append("")
        lines.append(membership_note)
    return "\n".join(lines)


def _format_8k_view_card(
    ticker: str, rendered_filings: list[dict],
    total_items: int, days: int, membership_note: str,
) -> str:
    """Render the multi-filing 8-K view card."""
    lines: list[str] = [
        f"<b>📋 SEC 8-K · {html.escape(ticker)} · 过去 {days} 天</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"共 <b>{len(rendered_filings)}</b> 个 filings · "
        f"<b>{total_items}</b> 个 items",
    ]
    if membership_note:
        lines.append(membership_note)

    # Filings rendered newest first (SEC returns recent first)
    for entry in rendered_filings:
        f = entry["filing"]
        items = entry["items"]
        extracted = entry["extracted_5_02"]

        lines.append("")
        amendment_tag = " <i>(amendment)</i>" if f.is_amendment else ""
        lines.append(
            f"<b>{html.escape(f.filing_date)}</b> · "
            f"<code>{html.escape(f.accession_number)}</code>{amendment_tag}"
        )

        for it in items:
            emoji = _TIER_EMOJI.get(it.priority_tier, "📌")
            annotation = ""
            if it.code == "2.02":
                annotation = " <i>(⑧ 处理)</i>"
            lines.append(
                f"  {emoji} <code>{html.escape(it.code)}</code> "
                f"[{it.priority_tier}] {html.escape(it.description)}{annotation}"
            )
            if it.code == "5.02":
                lines.extend(_format_5_02_extract_lines(extracted))

    return "\n".join(lines)


def _format_5_02_extract_lines(extracted: dict) -> list[str]:
    """5.02 LLM extraction sub-block (departures + appointments).

    Empty extracted dict → "(姓名待解析)" placeholder per Stage 4 spec
    (covers both LLM-failure and silent-no-people cases).
    """
    if not extracted:
        return ["       <i>(姓名待解析)</i>"]

    departures = extracted.get("departures") or []
    appointments = extracted.get("appointments") or []
    out: list[str] = []
    for p in departures[:3]:
        name = html.escape(str(p.get("name", "") or ""))
        title = html.escape(str(p.get("title", "") or ""))
        if name:
            out.append(f"       离职: <b>{name}</b> ({title})")
    for p in appointments[:3]:
        name = html.escape(str(p.get("name", "") or ""))
        title = html.escape(str(p.get("title", "") or ""))
        if name:
            out.append(f"       任命: <b>{name}</b> ({title})")
    if not out:
        out.append("       <i>(姓名待解析)</i>")
    return out


def _format_insider_empty_card(ticker: str, days_back: int, membership_note: str) -> str:
    lines = [
        f"<b>📥 内部人交易摘要 · {html.escape(ticker)} · 过去 {days_back} 天</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        "<i>无 Form 4 申报</i>",
    ]
    if membership_note:
        lines.append("")
        lines.append(membership_note)
    return "\n".join(lines)


def _format_insider_view_card(
    ticker: str, days_back: int, *,
    purchases: list, sales: list,
    noise_counts: dict[str, int],
    clusters: list,
    membership_note: str,
) -> str:
    """Render the multi-Form4 insider summary card."""
    lines: list[str] = [
        f"<b>📥 内部人交易摘要 · {html.escape(ticker)} · 过去 {days_back} 天</b>",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    if membership_note:
        lines.append(membership_note)
        lines.append("")

    # Purchase block
    if purchases:
        total_p = sum((t.transaction_usd or 0.0) for t in purchases)
        lines.append(
            f"<b>P (Purchase): {len(purchases)} 笔</b>, 总 {_fmt_money_kb(total_p)}"
        )
        biggest_p = max(purchases, key=lambda t: (t.transaction_usd or 0.0))
        lines.append(f"  最大: {_fmt_tx_one_liner(biggest_p)}")
    else:
        lines.append("<b>P (Purchase): 0 笔</b>")

    # Sale block
    if sales:
        total_s = sum((t.transaction_usd or 0.0) for t in sales)
        lines.append(
            f"<b>S (Sale):</b> {len(sales)} 笔, 总 {_fmt_money_kb(total_s)}"
        )
        biggest_s = max(sales, key=lambda t: (t.transaction_usd or 0.0))
        lines.append(f"  最大: {_fmt_tx_one_liner(biggest_s)}")
    else:
        lines.append("<b>S (Sale):</b> 0 笔")

    # Noise counts (A/M/F/G/C)
    if noise_counts:
        lines.append("")
        noise_labels = {
            "A": "Award (薪酬授予)",
            "M": "Exercise (option vest)",
            "F": "Tax (RSU vest 税款)",
            "G": "Gift (赠予)",
            "C": "Conversion (转换)",
        }
        for code in ("A", "M", "F", "G", "C"):
            n = noise_counts.get(code, 0)
            if n > 0:
                label = noise_labels[code]
                lines.append(f"<b>{code}</b>: {n} 笔  <i>({label})</i>")

    # Clusters
    lines.append("")
    if not clusters:
        lines.append(
            f"<b>集群:</b> 无同日 ≥3 distinct insiders 集群 ({days_back} 天内)"
        )
    else:
        lines.append(f"<b>集群:</b> {len(clusters)} 个")
        for c in clusters[:3]:
            direction_label = "买入" if c.direction == "purchase" else "卖出"
            names_preview = ", ".join(html.escape(n) for n in c.insider_names[:4])
            more = f", +{len(c.insider_names) - 4}" if len(c.insider_names) > 4 else ""
            lines.append(
                f"  {html.escape(c.cluster_date)} · {c.transaction_count} 笔 / "
                f"{len(c.insider_names)} 人 {direction_label} · "
                f"{_fmt_money_kb(c.total_usd)}"
            )
            lines.append(f"    {names_preview}{more}")

    return "\n".join(lines)


def _fmt_money_kb(v: float | None) -> str:
    """USD formatter for insider view — K / M units."""
    if v is None:
        return "未披露"
    abs_v = abs(v)
    if abs_v >= 1_000_000:
        return f"${v / 1_000_000:.2f}M"
    if abs_v >= 1_000:
        return f"${v / 1_000:.1f}K"
    return f"${v:,.0f}"


def _fmt_tx_one_liner(tx) -> str:
    """One-line description of a Form 4 transaction for the view card."""
    name = html.escape(tx.insider_name or "?")
    role_tag = f" ({html.escape(tx.insider_role)})" if tx.insider_role else ""
    plan = " · 10b5-1" if tx.is_10b5_1 else ""
    usd = _fmt_money_kb(tx.transaction_usd)
    date_s = html.escape(tx.transaction_date or tx.filing.filing_date)
    return f"{name}{role_tag} · {usd} · {date_s}{plan}"


# ---------------------------------------------------------------------------
# /earnings helpers — read user universe (held + watchlist)
# ---------------------------------------------------------------------------


def _user_universe() -> tuple[set[str], set[str]]:
    """Return ``(held, watchlist)`` ticker sets.

    Alpaca unavailable → empty held set, not a crash. Watchlist comes from
    the bot's own SQLite (no network).
    """
    from v2.bot import state as bot_state

    watchlist = {row["ticker"].upper() for row in bot_state.watchlist_list()}

    held: set[str] = set()
    try:
        from v2.broker import AlpacaUnavailable, get_portfolio
        snap = get_portfolio()
        held = {p["symbol"].upper() for p in snap.get("positions", [])}
    except Exception as exc:
        # Includes AlpacaUnavailable. Held stays empty; not a failure.
        logger.info("earnings: alpaca unavailable, watchlist-only: %s", exc)

    return held, watchlist


def _ticker_membership(ticker: str) -> tuple[bool, bool]:
    """Return ``(is_held, is_watchlist)``. Held trumps watchlist for badges."""
    held, watchlist = _user_universe()
    is_held = ticker in held
    is_watchlist = (ticker in watchlist) and not is_held
    return is_held, is_watchlist


# ---------------------------------------------------------------------------
# Internal formatting helpers (duplicate the formatter helpers locally so
# responders don't reach into v2/reporting internals)
# ---------------------------------------------------------------------------


def _short_money(v: float | None) -> str:
    if v is None:
        return "—"
    if v >= 1e12:
        return f"{v / 1e12:.1f}T"
    if v >= 1e9:
        return f"{v / 1e9:.1f}B"
    if v >= 1e6:
        return f"{v / 1e6:.1f}M"
    return f"{v:,.0f}"


def _pct1(v: float | None, *, signed: bool = False) -> str:
    if v is None:
        return "—"
    return f"{v:+.1%}" if signed else f"{v:.1%}"


def _fmt_change(v: float | None) -> str:
    if v is None:
        return ""
    emoji = "🟢" if v > 0 else ("🔴" if v < 0 else "🟡")
    return f"{emoji} <b>{v:+.2%}</b>"
