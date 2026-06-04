"""Bot application launcher — registers handlers and runs the polling loop."""

from __future__ import annotations

import logging
import os

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
)

from v2.bot import commands

logger = logging.getLogger(__name__)


def build_application() -> Application:
    """Wire up the bot — call build_application().run_polling() to launch."""
    token = os.environ["TELEGRAM_BOT_TOKEN"]

    app = Application.builder().token(token).build()

    # Stage 1 commands (watchlist + meta)
    app.add_handler(CommandHandler("start", commands.cmd_start))
    app.add_handler(CommandHandler("help", commands.cmd_help))
    app.add_handler(CommandHandler("watchlist", commands.cmd_watchlist))
    app.add_handler(CommandHandler("add", commands.cmd_add))
    app.add_handler(CommandHandler("remove", commands.cmd_remove))

    # Stage 2 action commands (FD + LLM-heavy)
    app.add_handler(CommandHandler("why", commands.cmd_why))
    app.add_handler(CommandHandler("summary", commands.cmd_summary))
    app.add_handler(CommandHandler("chain", commands.cmd_chain))
    app.add_handler(CommandHandler("13f", commands.cmd_13f))
    app.add_handler(CommandHandler("holders", commands.cmd_holders))
    app.add_handler(CommandHandler("etf", commands.cmd_etf))
    app.add_handler(CommandHandler("alert", commands.cmd_alert))
    app.add_handler(CommandHandler("alerts", commands.cmd_alerts))
    app.add_handler(CommandHandler("alert_remove", commands.cmd_alert_remove))
    app.add_handler(CommandHandler("portfolio", commands.cmd_portfolio))
    app.add_handler(CommandHandler("pnl", commands.cmd_pnl))
    app.add_handler(CommandHandler("settings", commands.cmd_settings))
    app.add_handler(CommandHandler("earnings", commands.cmd_earnings))

    # Stage 3 — NL intent classifier + dispatch
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, commands.cmd_nl)
    )

    return app


def run_bot() -> None:
    """Start the long-polling loop. Blocks until killed."""
    app = build_application()
    logger.info("Bot starting (polling mode)...")
    app.run_polling(allowed_updates=["message"])
