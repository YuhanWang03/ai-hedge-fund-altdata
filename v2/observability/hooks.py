"""Monkey-patch v2 modules so external calls emit trace events.

Design:
- Nothing is patched at import time. The dashboard backend explicitly calls
  install_all() during startup.
- Each individual patch is wrapped in try/except. A missing optional module
  (e.g. v2.data on a machine where only the bot is installed) reduces the
  hook count but never raises.
- install_all() is idempotent: a sentinel attribute on the patched function
  prevents double-wrapping if called twice (which would silently double-emit
  every event).
- All wrappers check current_trace() first. If no trace is bound (production
  path), the wrapper is a thin pass-through that runs the original function
  unchanged.

Returns a list[str] of hook names actually installed so callers can log /
assert in tests.
"""

from __future__ import annotations

import functools
import importlib
import logging
import time
from typing import Any, Callable

from v2.observability import pricing
from v2.observability.trace import current_trace, emit

logger = logging.getLogger(__name__)

_INSTALLED_SENTINEL = "__v2_observability_wrapped__"
_installed: list[str] = []


def installed_hooks() -> list[str]:
    """Names of hooks that were installed on the most recent install_all()."""
    return list(_installed)


def _already_wrapped(fn: Callable) -> bool:
    return getattr(fn, _INSTALLED_SENTINEL, False)


def _mark_wrapped(wrapper: Callable, original: Callable) -> Callable:
    setattr(wrapper, _INSTALLED_SENTINEL, True)
    setattr(wrapper, "__wrapped_original__", original)
    return wrapper


def _patch_method(cls: type, name: str, make_wrapper: Callable[[Callable], Callable]) -> bool:
    """Replace cls.name with make_wrapper(original). Returns True if patched."""
    original = getattr(cls, name, None)
    if original is None:
        return False
    if _already_wrapped(original):
        return False
    wrapper = _mark_wrapped(make_wrapper(original), original)
    setattr(cls, name, wrapper)
    return True


# ---------------------------------------------------------------------------
# FD client (CachedFDClient.get_* methods)
# ---------------------------------------------------------------------------

def _wrap_fd_method(endpoint_name: str):
    def make_wrapper(original):
        @functools.wraps(original)
        def wrapper(self, *args, **kwargs):
            trace = current_trace()
            if trace is None:
                return original(self, *args, **kwargs)

            t0 = time.perf_counter()
            cache_state = "unknown"
            try:
                result = original(self, *args, **kwargs)
                cache_state = "hit" if getattr(self, "_last_was_cache_hit", False) else "miss"
                return result
            finally:
                elapsed_ms = int((time.perf_counter() - t0) * 1000)
                ticker = (
                    args[0] if args else kwargs.get("ticker") or kwargs.get("symbol")
                )
                emit(
                    "api_call",
                    provider="fd",
                    endpoint=endpoint_name,
                    ticker=ticker,
                    cache=cache_state,
                    elapsed_ms=elapsed_ms,
                )
        return wrapper
    return make_wrapper


def _patch_fd_client() -> int:
    """Patch every public get_* / fetch_* method on CachedFDClient.

    The README documents v2.data as the FD client home but actual class
    layout may evolve; we discover methods by prefix rather than hardcoding.
    """
    try:
        mod = importlib.import_module("v2.data")
    except Exception as exc:
        logger.debug("v2.data not importable, skipping FD hooks: %s", exc)
        return 0

    count = 0
    for cls_name in ("CachedFDClient", "FDClient"):
        cls = getattr(mod, cls_name, None)
        if cls is None:
            continue
        for attr in dir(cls):
            if not (attr.startswith("get_") or attr.startswith("fetch_")):
                continue
            method = getattr(cls, attr, None)
            if not callable(method):
                continue
            if _patch_method(cls, attr, _wrap_fd_method(f"{cls_name}.{attr}")):
                count += 1
    return count


# ---------------------------------------------------------------------------
# DeepSeek LLM (langchain_deepseek.ChatDeepSeek.invoke / .ainvoke)
# ---------------------------------------------------------------------------

def _llm_summarize(messages: Any) -> str:
    """Best-effort textual preview of a LangChain message list / string."""
    try:
        if isinstance(messages, str):
            return messages[:500]
        # LangChain typically passes a list[BaseMessage].
        parts = []
        for m in messages if isinstance(messages, (list, tuple)) else [messages]:
            content = getattr(m, "content", None) or str(m)
            parts.append(str(content))
        joined = "\n".join(parts)
        return joined[:500]
    except Exception:
        return "<unprintable>"


def _llm_response_preview(result: Any) -> tuple[str, int, int]:
    """Extract (preview, input_tokens, output_tokens) from a LangChain result."""
    preview = ""
    in_tok = 0
    out_tok = 0
    try:
        content = getattr(result, "content", None)
        if content is not None:
            preview = str(content)[:500]
        else:
            preview = str(result)[:500]
        meta = getattr(result, "usage_metadata", None) or {}
        in_tok = int(meta.get("input_tokens", 0) or 0)
        out_tok = int(meta.get("output_tokens", 0) or 0)
        if in_tok == 0 and out_tok == 0:
            # Older LangChain: response_metadata.token_usage
            rm = getattr(result, "response_metadata", None) or {}
            usage = rm.get("token_usage") or rm.get("usage") or {}
            in_tok = int(usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0) or 0)
            out_tok = int(usage.get("completion_tokens", 0) or usage.get("output_tokens", 0) or 0)
    except Exception:
        pass
    return preview, in_tok, out_tok


def _wrap_llm_invoke(original):
    @functools.wraps(original)
    def wrapper(self, messages, *args, **kwargs):
        trace = current_trace()
        if trace is None:
            return original(self, messages, *args, **kwargs)

        prompt_preview = _llm_summarize(messages)
        model = getattr(self, "model_name", None) or getattr(self, "model", "deepseek-chat")
        t0 = time.perf_counter()
        result = original(self, messages, *args, **kwargs)
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        response_preview, in_tok, out_tok = _llm_response_preview(result)
        cost = pricing.deepseek_cost(in_tok, out_tok)
        emit(
            "llm_call",
            provider="deepseek",
            model=str(model),
            prompt_preview=prompt_preview,
            response_preview=response_preview,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=round(cost, 6),
            elapsed_ms=elapsed_ms,
        )
        return result
    return wrapper


def _patch_deepseek() -> int:
    try:
        mod = importlib.import_module("langchain_deepseek")
    except Exception as exc:
        logger.debug("langchain_deepseek not importable: %s", exc)
        return 0

    cls = getattr(mod, "ChatDeepSeek", None)
    if cls is None:
        return 0
    count = 0
    if _patch_method(cls, "invoke", lambda orig: _wrap_llm_invoke(orig)):
        count += 1
    return count


# ---------------------------------------------------------------------------
# Tavily search (tavily.TavilyClient.search)
# ---------------------------------------------------------------------------

def _wrap_tavily_search(original):
    @functools.wraps(original)
    def wrapper(self, query, *args, **kwargs):
        trace = current_trace()
        if trace is None:
            return original(self, query, *args, **kwargs)
        t0 = time.perf_counter()
        num_results: int | None = None
        try:
            result = original(self, query, *args, **kwargs)
            if isinstance(result, dict):
                items = result.get("results")
                if isinstance(items, list):
                    num_results = len(items)
            return result
        finally:
            emit(
                "api_call",
                provider="tavily",
                endpoint="search",
                query=str(query)[:200],
                num_results=num_results,
                cost_usd=round(pricing.tavily_cost(1), 6),
                elapsed_ms=int((time.perf_counter() - t0) * 1000),
            )
    return wrapper


def _patch_tavily() -> int:
    try:
        mod = importlib.import_module("tavily")
    except Exception as exc:
        logger.debug("tavily not importable: %s", exc)
        return 0
    cls = getattr(mod, "TavilyClient", None)
    if cls is None:
        return 0
    return 1 if _patch_method(cls, "search", lambda orig: _wrap_tavily_search(orig)) else 0


# ---------------------------------------------------------------------------
# Intent classifier (v2.bot.intent.classify_intent)
# ---------------------------------------------------------------------------

def _patch_intent_classifier() -> int:
    try:
        mod = importlib.import_module("v2.bot.intent")
    except Exception as exc:
        logger.debug("v2.bot.intent not importable: %s", exc)
        return 0

    fn = getattr(mod, "classify_intent", None)
    if fn is None or _already_wrapped(fn):
        return 0

    @functools.wraps(fn)
    def wrapper(text, *args, **kwargs):
        trace = current_trace()
        if trace is None:
            return fn(text, *args, **kwargs)
        t0 = time.perf_counter()
        result = fn(text, *args, **kwargs)
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        # The classifier returns either an object or dict; introspect both.
        intent_name = getattr(result, "name", None) or (
            result.get("name") if isinstance(result, dict) else None
        )
        intent_args = getattr(result, "args", None) or (
            result.get("args") if isinstance(result, dict) else None
        )
        emit(
            "intent_classified",
            input_text=text[:200],
            intent=intent_name,
            args=intent_args,
            elapsed_ms=elapsed_ms,
        )
        return result

    setattr(mod, "classify_intent", _mark_wrapped(wrapper, fn))
    return 1


# ---------------------------------------------------------------------------
# Archive store (v2.archive.store.log_*  write functions)
# ---------------------------------------------------------------------------

def _wrap_db_write(db_name: str):
    def make_wrapper(original):
        @functools.wraps(original)
        def wrapper(*args, **kwargs):
            trace = current_trace()
            if trace is None:
                return original(*args, **kwargs)
            t0 = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                emit(
                    "db_write",
                    db=db_name,
                    fn=getattr(original, "__name__", "<unknown>"),
                    elapsed_ms=int((time.perf_counter() - t0) * 1000),
                )
        return wrapper
    return make_wrapper


def _patch_archive_store() -> int:
    try:
        mod = importlib.import_module("v2.archive.store")
    except Exception as exc:
        logger.debug("v2.archive.store not importable: %s", exc)
        return 0
    count = 0
    for attr in dir(mod):
        if not (attr.startswith("log_") or attr.startswith("insert_") or attr.startswith("write_")):
            continue
        fn = getattr(mod, attr)
        if not callable(fn) or _already_wrapped(fn):
            continue
        wrapper = _mark_wrapped(_wrap_db_write("archive")(fn), fn)
        setattr(mod, attr, wrapper)
        count += 1
    return count


# ---------------------------------------------------------------------------
# Public installer
# ---------------------------------------------------------------------------

def install_all() -> list[str]:
    """Install all monkey-patches. Idempotent. Returns list of hook tags."""
    global _installed
    _installed = []

    matrix = [
        ("fd", _patch_fd_client),
        ("deepseek", _patch_deepseek),
        ("tavily", _patch_tavily),
        ("intent", _patch_intent_classifier),
        ("archive", _patch_archive_store),
    ]
    for tag, fn in matrix:
        try:
            n = fn()
        except Exception as exc:
            logger.warning("observability hook %s failed: %s", tag, exc)
            n = 0
        if n > 0:
            _installed.append(f"{tag}:{n}")

    logger.info("observability hooks installed: %s", _installed or "<none>")
    return list(_installed)
