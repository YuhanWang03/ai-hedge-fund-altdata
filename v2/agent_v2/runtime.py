"""Construction helpers for live and explicitly empty Agent V2 runtimes."""

from __future__ import annotations

from dataclasses import replace

from v2.agent_v2.adapters import (
    register_lab_capabilities,
    register_legacy_capabilities,
    register_market_capabilities,
    register_research_capabilities,
    register_web_capability,
    TavilyWebSearchPort,
    WorkspaceLabPort,
)
from v2.agent_v2.catalog import CapabilityCatalog, default_catalog
from v2.agent_v2.execution import CapabilityRegistry
from v2.agent_v2.llm import LLMEvidenceSynthesizer, StructuredLLMPlanner
from v2.agent_v2.orchestrator import AgentV2, AgentV2Config
from v2.agent_v2.session import ShortTermSession


def build_live_registry(
    catalog: CapabilityCatalog | None = None,
    *,
    lab=None,
    web_search=None,
) -> CapabilityRegistry:
    """Register live capabilities; Lab and Web remain explicit injections."""
    registry = CapabilityRegistry(catalog or default_catalog())
    register_research_capabilities(registry)
    register_legacy_capabilities(registry)
    register_market_capabilities(registry)
    if lab is not None:
        register_lab_capabilities(registry, lab)
    if web_search is not None:
        register_web_capability(registry, web_search)
    return registry


def build_empty_registry(catalog: CapabilityCatalog | None = None) -> CapabilityRegistry:
    """A deterministic test/development registry with no external dependencies."""
    return CapabilityRegistry(catalog or default_catalog())


def build_live_agent(*, config: AgentV2Config | None = None, lab=None, web_search=None) -> AgentV2:
    """Build the in-process starter runtime; Lab, Web, and channel wiring stay opt-in."""
    catalog = default_catalog()
    return AgentV2(
        catalog=catalog,
        registry=build_live_registry(catalog, lab=lab, web_search=web_search),
        session=ShortTermSession(),
        config=config,
    )


def build_llm_agent(*, config: AgentV2Config | None = None, llm=None, lab=None, web_search=None) -> AgentV2:
    """Build V2 with the existing OpenAI-compatible client for planning/synthesis."""
    if llm is None:
        from v2.agent.llm import build_llm

        llm = build_llm()
    catalog = default_catalog()
    registry = build_live_registry(catalog, lab=lab, web_search=web_search)
    return AgentV2(
        catalog=catalog,
        registry=registry,
        planner=StructuredLLMPlanner(llm, catalog),
        synthesizer=LLMEvidenceSynthesizer(llm),
        session=ShortTermSession(),
        config=config,
    )


def build_workspace_agent(
    *,
    config: AgentV2Config | None = None,
    llm=None,
    use_llm: bool = True,
    enable_web: bool = False,
    news_provider=None,
) -> AgentV2:
    """Compose existing Research + Web Lab tools, with optional Tavily fallback.

    This constructor is intended for the Web backend, where ``app.routers`` is
    importable.  Passing ``enable_web=True`` is the explicit network opt-in;
    individual calls must still pass ``allow_web=True``.
    """

    effective_config = config or AgentV2Config()
    if enable_web and not effective_config.enable_web_fallback:
        effective_config = replace(effective_config, enable_web_fallback=True)
    lab = WorkspaceLabPort()
    web_search = TavilyWebSearchPort(news_provider) if enable_web else None
    if use_llm:
        return build_llm_agent(
            config=effective_config,
            llm=llm,
            lab=lab,
            web_search=web_search,
        )
    return build_live_agent(config=effective_config, lab=lab, web_search=web_search)
