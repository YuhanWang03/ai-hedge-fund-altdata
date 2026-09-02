"""POST /api/chat — free-form NL query → same intents the bot supports.

Classify (DeepSeek, strict-enum) → dispatch to a v2 responder → return the
HTML card (+ optional base64 chart). Both steps are blocking, so they run in
a threadpool to keep the event loop free.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from app.auth import require_owner

router = APIRouter(prefix="/api", tags=["chat"])


class ChatIn(BaseModel):
    text: str


@router.post("/chat", dependencies=[Depends(require_owner)])
async def chat(body: ChatIn) -> dict:
    from app.dispatch import dispatch
    from v2.bot.intent import classify

    parsed = await run_in_threadpool(classify, body.text)
    result = await run_in_threadpool(dispatch, parsed)
    return {"intent": parsed.get("intent"), "args": parsed, **result}
