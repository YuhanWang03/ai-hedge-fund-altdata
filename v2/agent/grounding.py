"""Numeric grounding check — the anti-hallucination guarantee, kept.

The bot's design rule was "the LLM never decides what to *say*, only which tool
to *call*". Letting a model write the final answer gives that rule up, so it has
to be replaced by something enforceable rather than simply dropped.

The replacement is narrow and mechanical: **every figure in the answer must
appear in some observation the run actually collected.** No judgement, no second
LLM, no cost. It cannot catch a wrong *claim* built out of right numbers, and it
is not meant to — it catches invented numbers, which is the failure mode that
matters when the output looks like a research note.

Two categories are exempt, both under-approximations chosen to keep the signal
honest rather than to flatter the score:

* structural small integers (ranks, counts, "top 3") and 4-digit years;
* figures the model derived by arithmetic, which are reported separately as
  ``derived`` rather than counted as grounded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# Matches 1,234.56 / 22.3% / $1.2B / -3.6 — the shapes that appear in the cards.
_NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def _normalise(token: str) -> str:
    return token.replace(",", "").lstrip("-").rstrip(".")


def extract_numbers(text: str) -> list[str]:
    return [_NUMBER.sub(lambda m: m.group(0), m.group(0)) for m in _NUMBER.finditer(text or "")]


@dataclass
class GroundingReport:
    """Per-answer verdict, reported as a metric rather than a hard gate."""

    total: int = 0
    grounded: int = 0
    ungrounded: list[str] = field(default_factory=list)
    exempt: int = 0

    @property
    def ratio(self) -> float:
        return 1.0 if self.total == 0 else self.grounded / self.total

    @property
    def ok(self) -> bool:
        return not self.ungrounded

    def summary(self) -> str:
        if self.total == 0:
            return "no figures in answer"
        state = "clean" if self.ok else f"{len(self.ungrounded)} unsupported"
        return f"{self.grounded}/{self.total} grounded ({self.ratio:.0%}) — {state}"


def check(
    answer: str,
    observations: str,
    *,
    exempt_below: int = 13,
    tolerate_years: bool = True,
) -> GroundingReport:
    """Verify every figure in ``answer`` traces back to ``observations``."""
    haystack = (observations or "").replace(",", "")
    report = GroundingReport()

    for raw in extract_numbers(answer):
        token = _normalise(raw)
        if not token:
            continue

        # Structural exemptions: ordinals, counts, years.
        try:
            as_float = float(token)
        except ValueError:
            continue
        if as_float.is_integer() and abs(as_float) < exempt_below:
            report.exempt += 1
            continue
        if tolerate_years and as_float.is_integer() and 1900 <= as_float <= 2100:
            report.exempt += 1
            continue

        report.total += 1
        # Substring match against the de-comma'd corpus: an answer quoting
        # "22.3%" from a card that printed "22.3%" matches; an invented "27.8%"
        # does not. Trailing-zero variants are checked too (3.60 vs 3.6).
        variants = {token, token.rstrip("0").rstrip("."), f"{as_float:g}"}
        if any(v and v in haystack for v in variants):
            report.grounded += 1
        else:
            report.ungrounded.append(raw)

    return report


# ---------------------------------------------------------------------------
# Diagnosis — what is the check actually rejecting?
# ---------------------------------------------------------------------------

#: Classification of one rejected figure, ordered from most benign to least.
#: Ratios were tried and removed: with dozens of numbers in the observations,
#: some a/b×100 lands within tolerance of almost any target by chance, which
#: launders fabricated figures into "legitimate arithmetic" — the exact error
#: this diagnosis exists to prevent.
FIGURE_KINDS = ("rounding", "sum", "difference", "unknown")


def _observation_numbers(observations: str, limit: int = 80) -> list[float]:
    """Distinct numeric values appearing in the observations."""
    seen: list[float] = []
    for token in extract_numbers(observations):
        try:
            value = abs(float(_normalise(token)))
        except ValueError:
            continue
        if value and value not in seen:
            seen.append(value)
        if len(seen) >= limit:
            break
    return seen


def _close(a: float, b: float, rel: float = 0.002, abs_tol: float = 0.005) -> bool:
    return abs(a - b) <= max(abs_tol, abs(b) * rel)


def classify_figure(figure: str, values: list[float]) -> str:
    """Explain *why* a figure failed to trace, rather than only that it did.

    "18% of answers were ungrounded" is not actionable: a model that invented a
    statistic and one that added two published numbers without showing its work
    look identical in that statistic, and they call for opposite responses —
    tighten the prompt, or relax the check. This splits them.

    The classification is a **hypothesis, not proof**. With dozens of numbers in
    the observations some combination will match by coincidence, so a "sum"
    label means "there exists an arithmetic explanation", not "the model did
    that arithmetic". Only ``unknown`` is safe to act on hard: nothing in the
    observations produces that figure by any of these routes, so it was almost
    certainly invented. Tolerances are tight and ratios are not attempted, both
    to keep coincidental matches down.
    """
    try:
        target = abs(float(_normalise(figure)))
    except ValueError:
        return "unknown"
    if not target:
        return "unknown"

    for value in values:
        if _close(target, value, rel=0.02, abs_tol=0.051):
            return "rounding"

    count = len(values)
    for i in range(count):
        for j in range(i + 1, count):
            if _close(target, values[i] + values[j]):
                return "sum"
            if _close(target, abs(values[i] - values[j])):
                return "difference"

    # Triples are common in "top three combined" style claims but quadratic ×
    # linear gets slow, so only try them on a small observation set.
    if count <= 45:
        for i in range(count):
            for j in range(i + 1, count):
                partial = values[i] + values[j]
                for k in range(j + 1, count):
                    if _close(target, partial + values[k]):
                        return "sum"
    return "unknown"


def diagnose(report: GroundingReport, observations: str) -> dict[str, list[str]]:
    """Group a report's rejected figures by why they failed."""
    values = _observation_numbers(observations)
    grouped: dict[str, list[str]] = {kind: [] for kind in FIGURE_KINDS}
    for figure in report.ungrounded:
        grouped[classify_figure(figure, values)].append(figure)
    return {kind: figures for kind, figures in grouped.items() if figures}


def repair_instruction(report: GroundingReport) -> str:
    """Message appended for the one repair round when figures don't trace."""
    return (
        "GROUNDING CHECK FAILED. These figures in your answer do not appear in "
        f"any tool result from this run: {', '.join(report.ungrounded[:12])}.\n"
        "Rewrite the answer using only figures present in the observations above. "
        "If a number was your own arithmetic, show the inputs it came from. "
        "If you cannot support it, drop the claim — omitting a number is always "
        "better than inventing one."
    )
