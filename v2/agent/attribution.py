"""Attribution check — the number is real, but is it about the right company?

The grounding check asks "does this figure exist in the observations". That
catches invention, and after three evaluation rounds it catches it well. It is
blind to the failure underneath, which is worse in a finance product:

    用户问 ARKQ 的持仓 → 工具只有 ARKK → 模型报出 "TSLA 9.80%"
    用户问每只持仓的机构比例 → 只有 NVDA 有数据 → 模型给每只都套上 "Vanguard 8.94%"

Every figure there is real and traceable. The subject is wrong. Two rounds of
prompt hardening did not move it (h04 and h07 stayed at 0/3), which is the
expected outcome: a rule the verifier does not enforce is a rule the model does
not follow. So it becomes a check.

The mechanism is ownership. Each tool result was fetched *for* an entity — the
ticker, symbol or manager in its arguments — so the figures inside it belong to
that entity. A figure printed next to a different entity's name, when it belongs
to someone else and appears nowhere entity-neutral, is a misattribution.

Deliberately conservative, because a false positive here rejects a correct
answer: only figures that sit close after an entity mention are considered, a
figure owned by nobody in particular is always fine, and a figure the same
entity also owns is fine even if others own it too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from v2.agent.grounding import _normalise, extract_numbers

#: Arguments that name the subject a tool result is about.
_ENTITY_ARGS = ("ticker", "symbol", "manager")

#: Uppercase tickers plus the manager aliases the 13F tool accepts.
_ENTITY_MENTION = re.compile(r"\b[A-Z]{2,5}\b|巴菲特|buffett|burry|ark", re.IGNORECASE)

_NOT_ENTITIES = frozenset({
    "AI", "US", "USD", "CEO", "CFO", "COO", "CTO", "SEC", "ETF", "IPO", "EPS",
    "PE", "PB", "ROE", "GDP", "CPI", "PCE", "NFP", "PPI", "FOMC", "FED", "RSI",
    "CMF", "OK", "VS", "AND", "THE", "FOR", "NL", "LLM", "API", "MD", "P&L",
})

#: How far after an entity mention a figure is still "about" that entity.
WINDOW = 60


#: Sentence boundaries, plus phrases that acknowledge missing data.
_SENTENCE = re.compile(r"[。！？；;\n]")
_ACKNOWLEDGES_GAP = re.compile(
    r"没有|未记录|缺失|不可用|无数据|查不到|取不到|无法获取|暂无|未覆盖")


@dataclass
class AttributionReport:
    checked: int = 0
    misattributed: list[tuple[str, str, tuple[str, ...]]] = field(default_factory=list)
    #: Entities that were queried, returned nothing, and are still presented
    #: as having data. (entity, the figure it was given)
    empty_presented: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.misattributed and not self.empty_presented

    def summary(self) -> str:
        if self.ok:
            return f"{self.checked} 处主体-数字配对，无张冠李戴"
        parts = []
        if self.misattributed:
            pairs = ", ".join(f"{entity}←{figure}(实为 {'/'.join(owners)})"
                              for entity, figure, owners in self.misattributed[:3])
            parts.append(f"{len(self.misattributed)} 处张冠李戴：{pairs}")
        if self.empty_presented:
            pairs = ", ".join(f"{e}←{f}" for e, f in self.empty_presented[:3])
            parts.append(f"{len(self.empty_presented)} 处「无数据主体被写成有数据」：{pairs}")
        return "；".join(parts)


def _entity_of(args: dict[str, Any]) -> str:
    for key in _ENTITY_ARGS:
        value = str((args or {}).get(key, "")).strip()
        if value:
            return value.upper()
    return ""


def _mentions(text: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for match in _ENTITY_MENTION.finditer(text or ""):
        token = match.group(0).upper()
        if token not in _NOT_ENTITIES:
            out.append((token, match.end()))
    return out


def check(
    answer: str,
    results: Iterable[tuple[str, dict[str, Any], str, bool]],
) -> AttributionReport:
    """Flag figures printed against an entity that owns them nowhere.

    Args:
        answer: the model's final text.
        results: (tool name, arguments, content, ok) for each tool call made.
    """
    owners: dict[str, set[str]] = {}
    neutral: set[str] = set()

    for _tool, args, content, ok in results:
        if not ok:
            continue
        entity = _entity_of(args)
        # Entities named *inside* a result own the figures beside them too.
        # ARKK's holdings card prints "TSLA 9.80%": that weight belongs to ARKK
        # as a row and to TSLA as its subject, and an answer may legitimately
        # attribute it to either. Without this, every correct quotation of a
        # holdings or 13F card would be flagged.
        inner: dict[str, set[str]] = {}
        inner_mentions = _mentions(content)
        for index, (name, position) in enumerate(inner_mentions):
            limit = position + WINDOW
            if index + 1 < len(inner_mentions):
                nxt = inner_mentions[index + 1]
                limit = min(limit, nxt[1] - len(nxt[0]))
            for token in extract_numbers(content[position: max(limit, position)]):
                key = _normalise(token)
                if key:
                    inner.setdefault(key, set()).add(name)

        for token in extract_numbers(content):
            key = _normalise(token)
            if not key:
                continue
            holders = inner.get(key, set())
            if entity:
                holders = holders | {entity}
            if holders:
                owners.setdefault(key, set()).update(holders)
            else:
                neutral.add(key)

    # Entities that were asked about and came back with nothing quantitative.
    # h04 is this failure in its purest form: ARKQ has no card, so the model
    # answered with ARKK's holdings under ARKQ's name. Every figure is correctly
    # attributed to its own stock — the *frame* is what is false, and only the
    # emptiness of ARKQ's own result reveals it.
    empty_entities = {
        _entity_of(args)
        for _tool, args, content, ok in results
        if ok and _entity_of(args) and not extract_numbers(content)
    } - set(owners) - {""}

    report = AttributionReport()
    body = answer or ""
    mentions = _mentions(body)

    for sentence in _SENTENCE.split(body):
        if _ACKNOWLEDGES_GAP.search(sentence):
            continue                       # "ARKQ 没有数据，ARKK 的是…" is correct
        present = {name for name, _ in _mentions(sentence)} & empty_entities
        if not present:
            continue
        figures = extract_numbers(sentence)
        for entity in sorted(present):
            for figure in figures[:3]:
                report.empty_presented.append((entity, figure))
    for index, (entity, position) in enumerate(mentions):
        # A figure belongs to the nearest entity named before it, so the window
        # stops at the next mention. Without this, "NVDA 占仓 18.2%，CRWD 占仓
        # 22.4%" reads CRWD's weight as NVDA's and flags a correct sentence.
        limit = position + WINDOW
        if index + 1 < len(mentions):
            next_start = mentions[index + 1][1] - len(mentions[index + 1][0])
            limit = min(limit, next_start)
        window = body[position: max(limit, position)]
        for token in extract_numbers(window):
            key = _normalise(token)
            if not key or key in neutral:
                continue
            holders = owners.get(key)
            if not holders:
                continue                    # nobody owns it — grounding's problem
            report.checked += 1
            if entity not in holders:
                report.misattributed.append((entity, token, tuple(sorted(holders))))
    return report


def repair_instruction(report: AttributionReport) -> str:
    lines = [
        "ATTRIBUTION CHECK FAILED. 下面这些数字确实存在于工具返回里，"
        "但它们属于**另一个主体**，不能安在你写的那个身上："
    ]
    for entity, figure, holders in report.misattributed[:8]:
        lines.append(f"  · 你把 {figure} 写在 {entity} 名下，但它来自 {'/'.join(holders)}")
    for entity, figure in report.empty_presented[:6]:
        lines.append(f"  · {entity} 的工具返回里没有任何数据，你却给它写了 {figure}")
    lines.append(
        "重写：只用确实属于该主体的数据。某个主体没有数据，就明说"
        "「没有 X 的数据」——承认缺失永远好过换一个主体的数字顶上。")
    return "\n".join(lines)
