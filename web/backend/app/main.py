"""FastAPI entry for the new web backend (thin layer over v2 modules).

Personal, single-user operational panel + chat. Rebuilt from scratch; the
old dashboard/ is frozen. Run:

    cd web/backend
    WEB_OWNER_TOKEN=dev PYTHONPATH=.:../.. \
        uvicorn app.main:app --reload --port 8100
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import SETTINGS
from app.routers import chat, dashboard, health, portfolio

app = FastAPI(title="AI Hedge Fund · Web", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(SETTINGS.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(chat.router)
app.include_router(portfolio.router)
app.include_router(dashboard.router)
