"""B2 — an optional reading layer on top of the deterministic 10-Q MD&A diff.

``v2/sec/ten_q_parser.py`` already answers *what changed*: which MD&A paragraphs
are new against the prior quarter, how many risk-factor headings appeared, and
whether going-concern or material-weakness language is present. Those are
regex-and-diff facts, they escalate to P0 on their own, and **nothing here can
change or suppress them.**

What the pipeline cannot answer is *what the change means*. A new paragraph about
"extended customer acceptance cycles" is three lines of legalese that a reader
either recognises or scrolls past. That reading is open-ended, so it is where a
model belongs — and only as an addition.

## Why this one has no tools

The earlier pieces in this package are tool-calling loops because their questions
could only be answered by fetching things. This question cannot: the text is
already in hand, and the earnings figures come in with it. Wiring tools in so
that B2 also "looks like an agent" would add latency, tokens and failure modes to
buy nothing. It is one constrained call, and the interesting engineering is in
what happens to its output.

## The verification, which is stricter than anywhere else in this package

Elsewhere a claim is grounded when its *figures* trace to a tool result. Here the
model is reading prose, so numbers are not the risk — paraphrase is. A summary
that subtly restates a disclosure as worse (or better) than it is, is exactly the
failure that would matter in an earnings push.

So every reading must carry a **verbatim quote** from the paragraphs the model
was shown, checked by substring match after whitespace normalisation, against the
exact corpus it saw rather than the full filing. A reading whose quote cannot be
found is discarded — not repaired, not softened. Direction is a closed enum, and
figures in the reading still go through the numeric check as well.

Any failure returns an outcome with no points, and the card ships exactly as the
deterministic pipeline produced it.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from v2.agent import grounding
from v2.agent.anomaly_assist import run_with_deadline
from v2.agent.llm import LLMClient, build_llm

VALID_DIRECTIONS = frozenset({"利空", "利好", "中性"})

#: A "quote" shorter than this proves nothing — common phrases match by accident.
MIN_QUOTE_CHARS = 8


class TenQDeltaLike(Protocol):
    """Structural type for v2.sec.ten_q_parser.TenQDelta."""

    ticker: str
    period: str
    mda_added_paragraphs: list[str]
    new_risk_factor_count: int
    has_going_concern: bool
    has_material_weakness: bool


@dataclass(frozen=True)
class ReaderConfig:
    max_paragraphs: int = 6          # how many added paragraphs the model sees
    max_paragraph_chars: int = 900   # per-paragraph clip
    max_points: int = 3              # readings kept
    deadline_seconds: float = 20.0
    hard_deadline_grace: float = 5.0
    require_grounding: bool = True


@dataclass
class ReadingPoint:
    """One interpretation, anchored to text that provably exists."""

    quote: str
    reading: str
    direction: str = "中性"

    def render(self) -> str:
        icon = {"利空": "🔻", "利好": "🔺", "中性": "▫️"}.get(self.direction, "▫️")
        return f"{icon} 「{self.quote}」\n     → {self.reading}"


@dataclass
class ReadingOutcome:
    ticker: str
    points: list[ReadingPoint] = field(default_factory=list)
    outcome: str = "ok"      # ok | nothing_to_read | no_finding | unquoted |
                             # ungrounded | unparsable | timeout | error
    detail: str = ""
    tokens: int = 0
    elapsed_ms: int = 0
    rejected: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.outcome == "ok" and bool(self.points)

    def render(self) -> str:
        """The optional block appended under the deterministic 10-Q section."""
        if not self.ok:
            return ""
        body = "\n".join(p.render() for p in self.points)
        return f"<b>📖 MD&A 措辞解读</b>（agent 生成，引用均来自本次新增段落）\n{body}"


def enabled() -> bool:
    return os.environ.get("V2_AGENT_EARNINGS_READ", "").strip().lower() in (
        "1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Corpus + prompt
# ---------------------------------------------------------------------------

_WS = re.compile(r"\s+")


def _normalise(text: str) -> str:
    return _WS.sub("", text or "")


def build_corpus(delta: TenQDeltaLike, config: ReaderConfig) -> list[str]:
    """Exactly the paragraphs the model will see — and the only quote source.

    Verification runs against this, not the full filing: a quote can only be
    checked against what was actually shown, or the check would pass on text the
    model never read and could only have guessed at.
    """
    paragraphs = [p for p in (getattr(delta, "mda_added_paragraphs", None) or [])
                  if p and p.strip()]
    return [p.strip()[: config.max_paragraph_chars]
            for p in paragraphs[: config.max_paragraphs]]


_SYSTEM_PROMPT = """你在解读一份 10-Q 的 MD&A 相对上一季度的**新增段落**。

# 任务
指出这些新增措辞里，哪些对投资者是有意义的信号，以及它意味着什么。

# 硬性要求
1. 每条解读必须附一段**逐字摘自原文**的引用（quote）。一个字都不能改，
   不要改写、不要翻译、不要加省略号。引用长度至少 8 个字符。
2. 解读（reading）用一句中文说清楚这段措辞意味着什么，不要复述原文。
3. 方向（direction）只能是：利空 / 利好 / 中性。
4. 解读里如果出现数字，必须来自给你的材料。
5. **新增段落里如果没有真正有信息量的东西，就返回空列表。**
   例行的会计政策更新、模板化的前瞻性声明免责条款都属于没有信息量。
   宁可什么都不说，也不要为了凑数而过度解读。

# 输出
只输出 JSON，不要任何其他文字：
{"points": [{"quote": "原文逐字片段", "reading": "这意味着什么", "direction": "利空"}]}
没有值得说的就输出：{"points": []}
"""


def build_prompt(delta: TenQDeltaLike, corpus: list[str], facts: str = "") -> str:
    header = (
        f"公司：{getattr(delta, 'ticker', '?')}　报告期：{getattr(delta, 'period', '?')}\n"
        f"确定性管线已识别：新增风险因素 {getattr(delta, 'new_risk_factor_count', 0)} 条"
        f"　going concern：{'是' if getattr(delta, 'has_going_concern', False) else '否'}"
        f"　重大缺陷：{'是' if getattr(delta, 'has_material_weakness', False) else '否'}\n"
    )
    if facts:
        header += f"本季业绩：{facts}\n"
    body = "\n\n".join(f"【新增段落 {i}】\n{p}" for i, p in enumerate(corpus, 1))
    return f"{header}\n{body}"


# ---------------------------------------------------------------------------
# Parsing + verification
# ---------------------------------------------------------------------------

def _strip_fence(text: str) -> str:
    body = (text or "").strip()
    if body.startswith("```"):
        lines = body.split("\n")[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        body = "\n".join(lines).strip()
    return body


def parse_points(answer: str) -> tuple[list[ReadingPoint], str]:
    body = _strip_fence(answer)
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end <= start:
        return [], "no JSON object in answer"
    try:
        parsed = json.loads(body[start:end + 1])
    except json.JSONDecodeError as exc:
        return [], f"invalid JSON: {exc.msg}"
    if not isinstance(parsed, dict):
        return [], "top level is not an object"

    points: list[ReadingPoint] = []
    for item in parsed.get("points") or []:
        if not isinstance(item, dict):
            continue
        quote = str(item.get("quote", "")).strip().strip("「」\"'“”")
        reading = str(item.get("reading", "")).strip()
        if not quote or not reading:
            continue
        direction = str(item.get("direction", "中性")).strip()
        if direction not in VALID_DIRECTIONS:
            direction = "中性"
        points.append(ReadingPoint(quote=quote, reading=reading, direction=direction))
    return points, ""


def verify_quotes(points: list[ReadingPoint], corpus: list[str]) -> tuple[list[ReadingPoint], list[str]]:
    """Keep only readings whose quote is verbatim in what the model was shown."""
    haystack = _normalise("\n".join(corpus))
    kept: list[ReadingPoint] = []
    rejected: list[str] = []
    for point in points:
        if len(point.quote) < MIN_QUOTE_CHARS:
            rejected.append(f"引用过短：{point.quote}")
            continue
        if _normalise(point.quote) not in haystack:
            rejected.append(f"引用不在原文中：{point.quote[:40]}")
            continue
        kept.append(point)
    return kept, rejected


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def read(
    delta: TenQDeltaLike,
    *,
    facts: str = "",
    llm: LLMClient | None = None,
    config: ReaderConfig | None = None,
) -> ReadingOutcome:
    """Produce an optional reading of the MD&A diff. Never raises."""
    config = config or ReaderConfig()
    started = time.time()
    result = ReadingOutcome(ticker=getattr(delta, "ticker", "?"))

    corpus = build_corpus(delta, config)
    if not corpus:
        # No new paragraphs means nothing to interpret — and no cost.
        result.outcome = "nothing_to_read"
        result.detail = "本次没有新增 MD&A 段落"
        return result

    client = llm or build_llm()
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": build_prompt(delta, corpus, facts)},
    ]

    try:
        response, timed_out = run_with_deadline(
            lambda: client.complete(messages, None),
            config.deadline_seconds + config.hard_deadline_grace,
        )
    except Exception as exc:  # noqa: BLE001 — the cron must survive anything
        result.outcome = "error"
        result.detail = f"{type(exc).__name__}: {exc}"
        result.elapsed_ms = int((time.time() - started) * 1000)
        return result

    result.elapsed_ms = int((time.time() - started) * 1000)
    if timed_out or response is None:
        result.outcome = "timeout"
        result.detail = f"exceeded {config.deadline_seconds + config.hard_deadline_grace}s"
        return result

    result.tokens = response.total_tokens

    points, parse_error = parse_points(response.text)
    if parse_error:
        result.outcome = "unparsable"
        result.detail = parse_error
        return result
    if not points:
        # An empty list is a legitimate answer: most quarters' new paragraphs are
        # boilerplate, and saying nothing is the correct output for them.
        result.outcome = "no_finding"
        result.detail = "新增段落无实质信号"
        return result

    points, rejected = verify_quotes(points, corpus)
    result.rejected = rejected
    if not points:
        result.outcome = "unquoted"
        result.detail = "全部解读的引用都无法在原文中逐字找到"
        return result

    if config.require_grounding:
        report = grounding.check(" ".join(p.reading for p in points),
                                 "\n".join(corpus) + "\n" + facts)
        if not report.ok:
            result.outcome = "ungrounded"
            result.detail = "未溯源数字：" + ", ".join(report.ungrounded[:5])
            return result

    result.points = points[: config.max_points]
    return result


def read_if_enabled(delta: TenQDeltaLike | None, **kwargs: Any) -> ReadingOutcome | None:
    """Cron-facing wrapper: a no-op unless the flag is on and a diff exists."""
    if delta is None or not enabled():
        return None
    return read(delta, **kwargs)
