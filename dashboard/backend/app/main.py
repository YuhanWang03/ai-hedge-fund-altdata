"""FastAPI app entrypoint.

Wires together: SQLite store, observability hooks, session manager, routes.
Hooks installation is the single side-effecting step at startup — this is
the only place v2.observability.install_all() ever gets called.
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import SETTINGS
from app.db.store import open_store_factory
from app.routes import history, meta, query, trace
from app.runner.session import SessionManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def _ensure_v2_on_path() -> None:
    """The hedge-fund repo lives next to this backend on disk. We add it
    to sys.path so `import v2` resolves to the production codebase.
    """
    p = Path(SETTINGS.hedge_fund_repo_path).resolve()
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


@asynccontextmanager
async def lifespan(app: FastAPI):
    _ensure_v2_on_path()

    # Install observability hooks. Returns hook tags for /api/health.
    from v2.observability import install_all
    installed = install_all()
    app.state.installed_hooks = installed
    logger.info("observability hooks: %s", installed or "<none>")

    store = open_store_factory()
    await store.open()
    app.state.store = store
    app.state.session_manager = SessionManager()
    logger.info("dashboard backend ready on %s:%d", SETTINGS.host, SETTINGS.port)

    try:
        yield
    finally:
        await store.close()


app = FastAPI(
    title="AI Hedge Fund Dashboard",
    description="Trace view + Telegram-style chat over the v2 pipeline.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(SETTINGS.cors_origins) if SETTINGS.cors_origins else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(query.router, prefix="/api", tags=["query"])
app.include_router(trace.router, tags=["trace"])
app.include_router(history.router, prefix="/api", tags=["history"])
app.include_router(meta.router, prefix="/api", tags=["meta"])
