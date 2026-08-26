#!/usr/bin/env python3
"""
Model Router — task-aware model / provider selection for the SRE multi-agent system.

This module integrates project #6 from Harkirat Singh's "7 Projects" video
("Model router — chooses the right model for a task") into the SRE Agent.

Motivation
----------
The runtime already abstracts providers behind ``create_llm_with_error_handling``
(see ``llm_utils.py``), but every call in the OODA loop uses the *same* global
``LLM_PROVIDER`` and model. The workload, however, is heterogeneous:

- Supervisor **routing** and **narration** are cheap, high-frequency calls where
  a small/fast model is fine.
- The Reflector's **hypothesis** and the Planner's **remediation plan** are the
  high-stakes reasoning calls that justify a stronger (and pricier) model.

Routing each task to an appropriate model *tier* cuts cost and latency without
sacrificing quality on the calls that matter. This router makes that decision
explicit, deterministic, testable, and fully configurable via environment
variables — and it is a strict superset of the current behavior: when disabled
(``MODEL_ROUTER_ENABLED=false``) it falls back to the existing single-provider
path, so nothing breaks.

Design
------
``select_model()`` is pure logic (no LLM imports) so it is trivially unit-tested.
``route_llm()`` performs the same selection and then lazily delegates to the
existing ``create_llm_with_error_handling`` / ``create_llm_with_fallback``
machinery to actually construct the LLM.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Task types (the "what am I about to do?" dimension) ─────────────────────────
class TaskType(str, Enum):
    """The kind of work a given LLM call performs in the OODA loop."""

    ROUTING = "routing"          # supervisor picks the next specialist
    NARRATION = "narration"      # conversational handoff / greeting text
    GREETING = "greeting"        # casual follow-up acknowledgement
    SPECIALIST = "specialist"    # a specialist agent gathering evidence
    AGGREGATION = "aggregation"  # merging specialist findings into a summary
    REFLECTION = "reflection"    # ReflectorNode hypothesis formation (high stakes)
    PLANNING = "planning"        # PlannerNode remediation plan (high stakes)


# ── Model tiers (the "how much horsepower?" dimension) ──────────────────────────
class ModelTier(str, Enum):
    """Cost/capability tier a task is routed to."""

    FAST = "fast"          # cheap, low-latency, high-frequency calls
    BALANCED = "balanced"  # default working tier
    STRONG = "strong"      # highest-capability, high-stakes reasoning


# Default policy: which tier each task type wants. Deliberately conservative —
# only the two genuinely high-stakes reasoning steps escalate to STRONG.
_DEFAULT_POLICY: Dict[TaskType, ModelTier] = {
    TaskType.ROUTING: ModelTier.FAST,
    TaskType.NARRATION: ModelTier.FAST,
    TaskType.GREETING: ModelTier.FAST,
    TaskType.SPECIALIST: ModelTier.BALANCED,
    TaskType.AGGREGATION: ModelTier.BALANCED,
    TaskType.REFLECTION: ModelTier.STRONG,
    TaskType.PLANNING: ModelTier.STRONG,
}

# Ordering used when complexity bumps a task up a tier (and budget bumps down).
_TIER_ORDER: List[ModelTier] = [ModelTier.FAST, ModelTier.BALANCED, ModelTier.STRONG]

# Budget below which the router downgrades to the cheapest tier to conserve spend.
_LOW_BUDGET_THRESHOLD = float(os.getenv("MODEL_ROUTER_LOW_BUDGET_THRESHOLD", "1.0"))

# Suggested per-tier temperatures. Low temps for structured/high-stakes work,
# a touch more warmth for conversational narration.
_TIER_TEMPERATURE: Dict[ModelTier, float] = {
    ModelTier.FAST: 0.3,
    ModelTier.BALANCED: 0.1,
    ModelTier.STRONG: 0.1,
}


class ModelRouterBlocked(Exception):
    """Raised when the router refuses a request (off-policy or budget-exhausted)."""


@dataclass
class RequestContext:
    """Per-request signals the router uses beyond task type.

    This is what makes the router match the *product* definition (not OpenRouter):
    it routes by task complexity **and** by the caller's remaining budget, and it
    can **block** off-policy requests entirely.
    """

    remaining_budget: Optional[float] = None  # remaining credits/USD; None = unmetered
    off_policy: bool = False                   # caller-classified: disallowed request
    user_id: Optional[str] = None


@dataclass
class RoutingDecision:
    """The outcome of a routing decision — everything a caller needs to build an LLM."""

    task_type: TaskType
    tier: ModelTier
    provider: str
    temperature: float
    # ``model_id`` may be None, meaning "use the provider's default from
    # constants.py". It is only set when an explicit per-tier override exists.
    model_id: Optional[str] = None
    reason: str = ""
    blocked: bool = False
    block_reason: str = ""
    llm_kwargs: Dict = field(default_factory=dict)


def _router_enabled() -> bool:
    return os.getenv("MODEL_ROUTER_ENABLED", "true").lower() in ("true", "1", "yes")


def _default_provider() -> str:
    return os.getenv("LLM_PROVIDER", "groq")


def _escalate(tier: ModelTier, steps: int = 1) -> ModelTier:
    """Bump a tier up by ``steps`` positions, clamped at STRONG."""
    idx = min(_TIER_ORDER.index(tier) + steps, len(_TIER_ORDER) - 1)
    return _TIER_ORDER[idx]


def _downgrade(tier: ModelTier, steps: int = 1) -> ModelTier:
    """Bump a tier down by ``steps`` positions, clamped at FAST."""
    idx = max(_TIER_ORDER.index(tier) - steps, 0)
    return _TIER_ORDER[idx]


def _tier_provider(tier: ModelTier, default_provider: str) -> str:
    """Provider for a tier.

    By default every tier uses the same provider (``LLM_PROVIDER``), so the
    router degrades gracefully to a single-provider setup. Cross-provider
    routing is opt-in per tier via env vars, e.g.::

        MODEL_ROUTER_STRONG_PROVIDER=nvidia
        MODEL_ROUTER_FAST_PROVIDER=groq
    """
    return os.getenv(f"MODEL_ROUTER_{tier.value.upper()}_PROVIDER", default_provider)


def _tier_model_override(tier: ModelTier, provider: str) -> Optional[str]:
    """Explicit model id for a (tier, provider), if configured.

    Checked most-specific first so you can pin a model per provider *and* tier::

        MODEL_ROUTER_STRONG_MODEL_NVIDIA=meta/llama-3.3-70b-instruct
        MODEL_ROUTER_FAST_MODEL=llama-3.1-8b-instant

    Returns None when nothing is configured, in which case the provider's
    default model from ``constants.py`` is used.
    """
    specific = os.getenv(f"MODEL_ROUTER_{tier.value.upper()}_MODEL_{provider.upper()}")
    if specific:
        return specific
    return os.getenv(f"MODEL_ROUTER_{tier.value.upper()}_MODEL")


def select_model(
    task_type: TaskType,
    complexity: str = "simple",
    provider: Optional[str] = None,
    policy: Optional[Dict[TaskType, ModelTier]] = None,
    request: Optional[RequestContext] = None,
) -> RoutingDecision:
    """Decide which model tier / provider / model a task should use.

    Pure function — no LLM libraries imported — so it is cheap and easy to test.

    Routing considers three axes (matching the product definition, not OpenRouter):
    1. **Task complexity** — task type + simple/complex escalate the tier.
    2. **Budget** — a low remaining budget downgrades the tier (cheaper model);
       an exhausted budget blocks the request.
    3. **Policy** — an off-policy request is blocked outright.

    Args:
        task_type: What the LLM call is for (see :class:`TaskType`).
        complexity: "simple" or "complex". "complex" bumps the task up one tier.
        provider: Base provider override; defaults to ``LLM_PROVIDER`` env.
        policy: Optional task→tier policy override (defaults to the built-in one).
        request: Optional per-request budget/policy signals (see :class:`RequestContext`).

    Returns:
        A :class:`RoutingDecision` (check ``.blocked`` before using).
    """
    if isinstance(task_type, str):
        task_type = TaskType(task_type)

    base_provider = provider or _default_provider()
    active_policy = policy or _DEFAULT_POLICY

    # Off-policy requests are refused regardless of router state.
    if request and request.off_policy:
        return RoutingDecision(
            task_type=task_type, tier=ModelTier.FAST, provider=base_provider,
            temperature=_TIER_TEMPERATURE[ModelTier.FAST],
            blocked=True, block_reason="Request is off-policy and was blocked.",
            reason="blocked: off-policy",
        )

    # Exhausted budget is refused too.
    if request and request.remaining_budget is not None and request.remaining_budget <= 0:
        return RoutingDecision(
            task_type=task_type, tier=ModelTier.FAST, provider=base_provider,
            temperature=_TIER_TEMPERATURE[ModelTier.FAST],
            blocked=True, block_reason="Budget exhausted; request blocked.",
            reason="blocked: budget exhausted",
        )

    # Router off → everything on the BALANCED tier with the base provider. This
    # reproduces the pre-router single-model behavior.
    if not _router_enabled():
        return RoutingDecision(
            task_type=task_type,
            tier=ModelTier.BALANCED,
            provider=base_provider,
            temperature=_TIER_TEMPERATURE[ModelTier.BALANCED],
            model_id=None,
            reason="Model router disabled (MODEL_ROUTER_ENABLED=false); using base provider default.",
        )

    tier = active_policy.get(task_type, ModelTier.BALANCED)

    escalated = False
    if str(complexity).lower() == "complex":
        bumped = _escalate(tier, 1)
        if bumped != tier:
            escalated = True
        tier = bumped

    # Budget-constrained downgrade: conserve spend when running low.
    budget_downgraded = False
    if request and request.remaining_budget is not None and request.remaining_budget < _LOW_BUDGET_THRESHOLD:
        bumped_down = _downgrade(tier, 1)
        if bumped_down != tier:
            budget_downgraded = True
        tier = bumped_down

    tier_provider = _tier_provider(tier, base_provider)
    model_override = _tier_model_override(tier, tier_provider)

    reason = f"{task_type.value} → {tier.value} tier on '{tier_provider}'"
    if escalated:
        reason += " (escalated: complex task)"
    if budget_downgraded:
        reason += f" (downgraded: low budget < {_LOW_BUDGET_THRESHOLD})"
    if model_override:
        reason += f" (model={model_override})"

    return RoutingDecision(
        task_type=task_type,
        tier=tier,
        provider=tier_provider,
        temperature=_TIER_TEMPERATURE[tier],
        model_id=model_override,
        reason=reason,
    )


def route_llm(
    task_type: TaskType,
    complexity: str = "simple",
    provider: Optional[str] = None,
    use_fallback: bool = True,
    request: Optional[RequestContext] = None,
    **kwargs,
):
    """Select a model for ``task_type`` and build the LLM instance.

    Delegates construction to the existing ``llm_utils`` helpers (imported lazily
    so importing this module has no heavy dependencies). When ``use_fallback`` is
    True the router still benefits from the provider fallback chain, so a routed
    provider being unavailable degrades gracefully instead of failing hard.

    Raises:
        ModelRouterBlocked: if the request is off-policy or the budget is exhausted.

    Returns:
        An LLM instance, exactly as ``create_llm_with_error_handling`` would.
    """
    decision = select_model(task_type, complexity=complexity, provider=provider, request=request)
    if decision.blocked:
        logger.warning(f"ModelRouter BLOCKED: {decision.block_reason}")
        raise ModelRouterBlocked(decision.block_reason)
    logger.info(f"ModelRouter: {decision.reason}")

    # LiteLLM backend (optional): our tier decides the model; LiteLLM does the
    # multi-provider/cost/fallback plumbing. Falls through to the provider path
    # if not enabled, no tier model configured, or LiteLLM is unavailable.
    from .litellm_backend import build_litellm_llm, litellm_enabled, tier_litellm_model

    if litellm_enabled():
        model = tier_litellm_model(decision.tier.value)
        if model:
            try:
                return build_litellm_llm(model, temperature=decision.temperature, max_tokens=kwargs.get("max_tokens"))
            except Exception as e:
                logger.warning(f"LiteLLM backend unavailable ({e}); using provider path")

    # Lazy import: keeps ``select_model`` (and this module) importable without
    # langchain installed, which is what makes the unit tests dependency-free.
    from .llm_utils import create_llm_with_error_handling, create_llm_with_fallback

    llm_kwargs = dict(kwargs)
    llm_kwargs.setdefault("temperature", decision.temperature)
    if decision.model_id:
        llm_kwargs["model_id"] = decision.model_id

    if use_fallback:
        return create_llm_with_fallback(primary_provider=decision.provider, **llm_kwargs)
    return create_llm_with_error_handling(decision.provider, **llm_kwargs)
