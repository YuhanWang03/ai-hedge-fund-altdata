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
