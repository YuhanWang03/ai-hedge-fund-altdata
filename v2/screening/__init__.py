"""Fundamental screening — daily filter + LLM narration of tech stocks."""

from v2.screening.filters import DEFAULT_FILTERS, passes_filter
from v2.screening.models import FilterConfig, ScreenCandidate, ScreenResult
from v2.screening.narrator import narrate
from v2.screening.screener import build_candidate, run_screening
from v2.screening.universe import TECH_30

__all__ = [
    "DEFAULT_FILTERS",
    "FilterConfig",
    "ScreenCandidate",
    "ScreenResult",
    "TECH_30",
    "build_candidate",
    "narrate",
    "passes_filter",
    "run_screening",
]
