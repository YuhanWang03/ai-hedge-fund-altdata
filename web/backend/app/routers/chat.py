"""POST /api/chat — free-form NL query → same intents the bot supports.

Classify (DeepSeek, strict-enum) → dispatch to a v2 responder → return the
HTML card (+ optional base64 chart). Both steps are blocking, so they run in
a threadpool to keep the event loop free.
"""

from __future__ import annotations

import asyncio
import html as _html
import logging
import time
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from app.auth import require_owner

router = APIRouter(prefix="/api", tags=["chat"])
logger = logging.getLogger("web.chat")

# A responder that reaches a slow/unreachable external API (Tavily / OpenAI
# embeddings) could otherwise hang the request forever. Cap it.
_CLASSIFY_TIMEOUT = 25
_DISPATCH_TIMEOUT = 45
_CHAIN_JOB_TIMEOUT = 180
_JOB_TTL_SECONDS = 15 * 60

# The web request should not have to stay open for a multi-provider /chain run.
# Jobs are deliberately session-local: this remains a single-user workbench,
# and a process restart simply asks the user to run the research again.
_CHAT_JOBS: dict[str, dict] = {}
_RUNNING_TASKS: set[asyncio.Task[None]] = set()


class ChatIn(BaseModel):
    text: str


def _prune_jobs() -> None:
    cutoff = time.time() - _JOB_TTL_SECONDS
    stale = [job_id for job_id, job in _CHAT_JOBS.items()
             if job.get("updated_at", 0) < cutoff and job.get("status") != "running"]
    for job_id in stale:
        _CHAT_JOBS.pop(job_id, None)


def _running_chain_job(parsed: dict) -> tuple[str, dict] | None:
    ticker = parsed.get("ticker", "")
    for job_id, job in _CHAT_JOBS.items():
        if (job.get("status") == "running"
                and job.get("intent") == "chain"
                and job.get("ticker") == ticker):
            return job_id, job
    return None


async def _run_chain_job(job_id: str, parsed: dict) -> None:
    from app.dispatch import dispatch

    try:
        result = await asyncio.wait_for(
            run_in_threadpool(dispatch, parsed), timeout=_CHAIN_JOB_TIMEOUT,
        )
        _CHAT_JOBS[job_id].update({
            "status": "completed",
            "result": {"intent": "chain", "args": parsed, **result},
            "updated_at": time.time(),
        })
    except asyncio.TimeoutError:
        _CHAT_JOBS[job_id].update({
            "status": "failed",
            "error": "产业链研究超过 180 秒，请稍后重试。",
            "updated_at": time.time(),
        })
    except Exception as exc:
        logger.exception("chain job failed for %s", parsed.get("ticker", ""))
        _CHAT_JOBS[job_id].update({
            "status": "failed",
            "error": str(exc),
            "updated_at": time.time(),
        })


def _start_chain_job(parsed: dict) -> dict:
    _prune_jobs()
    existing = _running_chain_job(parsed)
    if existing:
        job_id, _ = existing
        return {"intent": "chain", "status": "running", "job_id": job_id,
                "deduplicated": True}

    job_id = uuid4().hex
    _CHAT_JOBS[job_id] = {
        "status": "running",
        "intent": "chain",
        "ticker": parsed.get("ticker", ""),
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    task = asyncio.create_task(_run_chain_job(job_id, parsed))
    _RUNNING_TASKS.add(task)
    task.add_done_callback(_RUNNING_TASKS.discard)
    return {"intent": "chain", "status": "running", "job_id": job_id,
            "deduplicated": False}


@router.get("/chat/jobs/{job_id}", dependencies=[Depends(require_owner)])
async def chat_job(job_id: str) -> dict:
    _prune_jobs()
    job = _CHAT_JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="chat job not found")
    if job["status"] == "completed":
        return {"job_id": job_id, "status": "completed", **job["result"]}
    if job["status"] == "failed":
        return {"job_id": job_id, "status": "failed", "intent": "chain",
                "error": job.get("error", "产业链研究失败")}
    return {"job_id": job_id, "status": "running", "intent": "chain",
            "ticker": job.get("ticker", "")}


@router.post("/chat", dependencies=[Depends(require_owner)])
async def chat(body: ChatIn) -> dict:
    from app.dispatch import dispatch, parse_slash

    try:
        # Slash commands (/flow AAPL, ...) skip the LLM classifier;
        # free-form text goes through it.
        parsed = parse_slash(body.text)
        if parsed is None:
            from v2.bot.intent import classify
            parsed = await asyncio.wait_for(
                run_in_threadpool(classify, body.text), timeout=_CLASSIFY_TIMEOUT,
            )

        if parsed.get("intent") == "chain":
            return _start_chain_job(parsed)

        result = await asyncio.wait_for(
            run_in_threadpool(dispatch, parsed), timeout=_DISPATCH_TIMEOUT,
        )
        return {"intent": parsed.get("intent"), "args": parsed, **result}
    except asyncio.TimeoutError:
        logger.warning("chat timed out for %r", body.text)
        return {
            "intent": "timeout",
            "html": (
                "⏱ 查询超时（某个外部数据源响应过慢）。<br>"
                "「为什么涨/跌」需要连 Tavily / OpenAI，本机网络可能受阻；"
                "可改用 <code>/flow</code>、左侧面板，或把网页部署到 VPS。"
            ),
        }
    except Exception as exc:
        # Surface the real error instead of an opaque 500 (full traceback
        # goes to the uvicorn console).
        logger.exception("chat failed for %r", body.text)
        return {
            "intent": "error",
            "html": f"❌ 出错: <code>{_html.escape(str(exc))}</code>",
            "error_type": type(exc).__name__,
        }
