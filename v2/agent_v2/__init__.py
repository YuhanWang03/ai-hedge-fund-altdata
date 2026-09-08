"""Agent V2: evidence-first orchestration over the project's existing engines.

This package intentionally has no production side effects at import time.  The
old :mod:`v2.agent` package remains the incumbent while this implementation is
built and evaluated alongside it.
"""

from v2.agent_v2.catalog import CapabilityCatalog, CapabilitySpec, default_catalog
from v2.agent_v2.models import (
    AgentResult,
    AnswerMode,
    BudgetClass,
    EvidenceItem,
    ExecutionPlan,
    PlanTask,
    RouteKind,
    RunStatus,
    ToolEnvelope,
)
from v2.agent_v2.orchestrator import AgentV2, AgentV2Config
from v2.agent_v2.runtime import build_live_agent, build_llm_agent, build_workspace_agent

__all__ = [
    "AgentResult",
    "AgentV2",
    "AgentV2Config",
    "AnswerMode",
    "BudgetClass",
    "CapabilityCatalog",
    "CapabilitySpec",
    "EvidenceItem",
    "ExecutionPlan",
    "PlanTask",
    "RouteKind",
    "RunStatus",
    "ToolEnvelope",
    "build_live_agent",
    "build_llm_agent",
    "build_workspace_agent",
    "default_catalog",
]
