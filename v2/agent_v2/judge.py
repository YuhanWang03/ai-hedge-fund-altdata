"""The claim judge: one model call that says whether an answer asserts what a rule forbids.

A wording rule used to be a regular expression over the answer ("申报内容未读取"),
and every paraphrase the model found slipped past it.  The judge takes the
rule as a sentence in plain language ("申报的正文没有被读取") and the text it
applies to, and answers whether the text asserts it, quoting the sentence
that does.  The decision of *when* a rule applies stays with the rules
(the attributor's reader did read the filings); only the language
judgement moves to the model.  All of an answer's claims go in one call.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_SYSTEM = """你是投研回答的表述审查员，只输出 JSON，不回答用户问题。
给你若干条目，每条有一段文本（text）和一个不允许出现的断言（claim）。
逐条判断：这段文本是否做出了这个断言（意思相同即算，措辞不必相同；带"可能、尚未确认、不能据此"这类限定的不算断言）。
做出了就 asserted=true 并原样引用做出断言的那句话（quote，30 字内可截断），没有就 asserted=false。
只输出：{"verdicts":[{"id":"...","asserted":true,"quote":"..."}]}"""


class ClaimJudge:
    """Callable: ``judge(items) -> {id: quote}`` for the items whose text asserts their claim."""

    #: Ledger source for the judge's calls.
    usage_source_name = "agent_v2.judge"

    def __init__(self, llm: Any, *, max_items: int = 24, max_text_chars: int = 1200) -> None:
        self.llm = llm
        self.max_items = max_items
        self.max_text_chars = max_text_chars

    def __call__(self, items: list[dict[str, str]]) -> dict[str, str]:
        rows = [{"id": str(item["id"]), "text": str(item["text"])[: self.max_text_chars], "claim": str(item["claim"])} for item in items[: self.max_items] if item.get("text") and item.get("claim")]
        if self.llm is None or not rows:
            return {}
        from v2.agent_v2.agents.base import strip_fence
        from v2.usage_context import usage_source

        try:
            with usage_source(self.usage_source_name):
                response = self.llm.complete([{"role": "system", "content": _SYSTEM}, {"role": "user", "content": json.dumps({"items": rows}, ensure_ascii=False)}], None)
            verdicts = json.loads(strip_fence(response.text)).get("verdicts") or []
        except Exception as exc:  # noqa: BLE001 — an unavailable judge means the rule is not applied, never a failed run
            logger.warning("claim judge failed: %s: %s", type(exc).__name__, exc)
            return {}
        known = {row["id"] for row in rows}
        asserted: dict[str, str] = {}
        for verdict in verdicts:
            if not isinstance(verdict, dict):
                continue
            key = str(verdict.get("id") or "")
            if key in known and verdict.get("asserted"):
                asserted[key] = " ".join(str(verdict.get("quote") or "").split())[:60]
        return asserted
