"""Agent loop over the existing responder tool surface.

New package, additive only: nothing under ``v2/bot`` is imported at module load
and nothing there is modified. The production Telegram path keeps its
single-hop, strict-enum behaviour; this package is a second front-end that lets
the model drive control flow, so the two can be compared on the same queries.

    from v2.agent import run_agent, run_baseline
    from v2.agent.fixtures import build_registry

    registry = build_registry()                     # no data keys needed
    print(run_agent("我持仓里哪只最危险", registry=registry).answer)

CLI:  python -m v2.agent.cli "我持仓里哪只最危险" --mode both --tools fixture
"""

from v2.agent.baseline import BaselineResult, run_baseline
from v2.agent.loop import AgentConfig, AgentResult, run_agent
from v2.agent.registry import (
    TOOL_SPECS,
    FixtureExecutor,
    LiveExecutor,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)

__all__ = [
    "AgentConfig",
    "AgentResult",
    "BaselineResult",
    "FixtureExecutor",
    "LiveExecutor",
    "TOOL_SPECS",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "run_agent",
    "run_baseline",
]
