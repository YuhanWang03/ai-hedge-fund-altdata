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

from v2.agent.grounding import (_normalise, extract_numbers,
                                extract_numbers_with_context)

#: Arguments that name the subject a tool result is about.
_ENTITY_ARGS = ("ticker", "symbol", "manager")

#: Uppercase tickers plus the manager aliases the 13F tool accepts.
#:
#: **No global IGNORECASE.** It was there for the aliases and silently applied to
#: the ticker alternative too, so every 2-5 letter word became an entity — "Form",
#: "Item", "paper" and "Tier" all showed up as owners in the first live run, and
#: correct answers were flagged. The aliases carry their own case variants instead.
_ENTITY_MENTION = re.compile(
    r"\b[A-Z]{2,5}\b|巴菲特|伯克希尔|木头姐|[Bb]uffett|[Bb]urry|BERKSHIRE")

#: Filing identifiers that read as numbers but name a document, not a quantity:
#: 8-K, 10-Q, 13F, Item 5.02. Masked out before extraction, on both sides, so
#: "SMCI 的 8-K 查询超时" cannot be read as attributing the value 8 to SMCI.
_FILING_TOKEN = re.compile(
    r"\b\d{1,2}-[A-Z]\b|\b\d{1,2}[FKQ]\b|(?:[Ii]tem|[Ss]ection)\s*\d+(?:\.\d+)?")

#: The 13F tool takes an alias while answers use the Chinese name; without
#: normalising, every figure next to 「巴菲特」 looks misattributed away from
#: the "BUFFETT" that owns it.
_ALIASES = {
    "巴菲特": "BUFFETT", "伯克希尔": "BUFFETT", "BERKSHIRE": "BUFFETT",
    "BURRY": "BURRY", "木头姐": "ARK", "ARKK": "ARKK",
}

_NOT_ENTITIES = frozenset({
    "AI", "US", "USD", "CEO", "CFO", "COO", "CTO", "SEC", "ETF", "IPO", "EPS",
    "PE", "PB", "ROE", "GDP", "CPI", "PCE", "NFP", "PPI", "FOMC", "FED", "RSI",
    "CMF", "OK", "VS", "AND", "THE", "FOR", "NL", "LLM", "API", "MD", "P&L",
})

#: How far after an entity mention a figure is still "about" that entity.
WINDOW = 60

#: Boundaries a figure never reaches back across. The window used to stop only
#: at the next entity mention, so the last entity on a line swallowed whatever
#: came after it:
#:
#:     · 已浮亏 -21.5%，今日 -5.40%，相对 SMH 逆势 -8.30pp
#:     · 2026-09-09 财报，上次 EPS miss -23.6%、财报后次日 -14.20%
#:
#: No entity follows SMH, so its 60 characters ran into the next bullet and
#: reported SMCI's earnings history as SMH's — a correct answer, rejected. A new
#: line or bullet starts a new subject.
#:
#: Only applied to the answer. On the observation side a narrower window would
#: record *fewer* owners, and fewer owners means more false positives; being
#: generous about who owns a figure is the conservative direction there.
_LAYOUT_BREAK = re.compile(r"[\n·•]")


#: Chinese puts a modifier before its head, so a figure can belong to the entity
#: that *follows* it: 「但被占仓 66.3% 的 IVV 微跌 -0.47% 抵消了大半」. Reading
#: left to right, 66.3 sits after NVDA and was reported as NVDA's — a correct
#: sentence, rejected. When a figure is joined to the next entity by a short
#: 「…的」, that entity is its subject, not the one before it.
_POSTPOSED = re.compile(r"^[^，。；、,;\n]{0,10}的\s*$")

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


def _is_structural(token: str, *, exempt_below: int = 13,
                   tolerate_years: bool = True) -> bool:
    """Ordinals, small counts and years carry no attribution risk.

    Grounding has exempted these from the start — "Top 1" and "过去 4 次" are
    labels, not measurements — but the attribution check did not, and the gap
    showed up live in the ugliest possible way. The check complained that IVV
    was given a "1"; the repair round dutifully wrote *「risk_view 里没有 IVV
    的「1」这个数据」*; that sentence puts a 1 right after IVV, so the check
    complained again about the apology it had just caused.

    Same thresholds as grounding, for the same reason: two checks disagreeing
    about what counts as a figure is a bug generator.
    """
    try:
        value = float(token)
    except ValueError:
        return False
    if value.is_integer() and abs(value) < exempt_below:
        return True
    return tolerate_years and value.is_integer() and 1900 <= value <= 2100


def _record(report: "AttributionReport", entity: str, token: str,
            holders: set[str]) -> None:
    report.checked += 1
    finding = (entity, token, tuple(sorted(holders)))
    if entity not in holders and finding not in report.misattributed:
        report.misattributed.append(finding)


def _window_end(text: str, start: int, limit: int) -> int:
    """Where an entity's window really ends: the first layout boundary in it."""
    if limit <= start:
        return start
    brk = _LAYOUT_BREAK.search(text, start, limit)
    return brk.start() if brk else limit


def _mask_filings(text: str) -> str:
    """Blank out filing identifiers so they are not read as quantities."""
    return _FILING_TOKEN.sub(lambda m: " " * len(m.group(0)), text or "")


def _entity_of(args: dict[str, Any]) -> str:
    for key in _ENTITY_ARGS:
        value = str((args or {}).get(key, "")).strip()
        if value:
            upper = value.upper()
            return _ALIASES.get(upper, upper)
    return ""


def _mentions(text: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for match in _ENTITY_MENTION.finditer(text or ""):
        token = match.group(0).upper()
        token = _ALIASES.get(token, token)
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

    for _tool, args, raw_content, ok in results:
        if not ok:
            continue
        content = _mask_filings(raw_content)
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
    body = _mask_filings(answer or "")
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
                if (entity, figure) not in report.empty_presented:
                    report.empty_presented.append((entity, figure))
    for index, (entity, position) in enumerate(mentions):
        # A figure belongs to the nearest entity named before it, so the window
        # stops at the next mention. Without this, "NVDA 占仓 18.2%，CRWD 占仓
        # 22.4%" reads CRWD's weight as NVDA's and flags a correct sentence.
        limit = position + WINDOW
        if index + 1 < len(mentions):
            next_start = mentions[index + 1][1] - len(mentions[index + 1][0])
            limit = min(limit, next_start)
        limit = _window_end(body, position, limit)
        window = body[position: max(limit, position)]
        next_entity, next_start = "", len(body)
        if index + 1 < len(mentions):
            next_entity, next_end = mentions[index + 1]
            next_start = next_end - len(next_entity)

        for token, _before, start, end in extract_numbers_with_context(window):
            key = _normalise(token)
            if not key or key in neutral or _is_structural(key):
                continue
            holders = owners.get(key)
            if not holders:
                continue                    # nobody owns it — grounding's problem
            # 「占仓 66.3% 的 IVV」: the figure modifies what comes after it.
            # The backward pass below checks it against that entity, so skipping
            # here reattributes rather than excuses.
            if next_entity and _POSTPOSED.match(body[position + end: next_start]):
                continue
            _record(report, entity, token, holders)

    # Backward pass: a figure joined to a mention by 「…的」 belongs to it, and
    # the forward windows cannot see it — they start *after* each mention, so a
    # figure in front of the first entity in a sentence is checked by nobody.
    for entity, end_pos in mentions:
        start_pos = end_pos - len(entity)
        prefix = body[max(0, start_pos - WINDOW): start_pos]
        figures = extract_numbers_with_context(prefix)
        if not figures:
            continue
        token, _before, _start, end = figures[-1]
        if not _POSTPOSED.match(prefix[end:]):
            continue
        key = _normalise(token)
        if not key or key in neutral or _is_structural(key):
            continue
        holders = owners.get(key)
        if holders:
            _record(report, entity, token, holders)
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
