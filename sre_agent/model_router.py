#!/usr/bin/env python3
"""
Model Router — task-aware model tier selection for the SRE multi-agent system.

Scope, stated plainly
---------------------
What is live: every production LLM call routes by :class:`TaskType` to a model
tier, resolved one rung up or down the Anthropic ladder from the cluster's own
anchor model.

What is not: this is **not** adaptive or data-driven routing, and it is not
cross-provider. ``SUPPORTED_PROVIDERS`` is ``("anthropic",)``; the per-tier
provider override is validated against it. ``complexity`` and
:class:`RequestContext` (budget, off-policy) are implemented and tested, but no
production caller passes either — every live call site uses the defaults. Until
a caller measures and supplies them, the honest claim is "static task tiers",
not "adaptive routing", and there is no evidence here that routing saves money.
Proving that needs a per-task cost/quality frontier and a router-vs-fixed-model
experiment, neither of which exists yet.

Motivation
----------
The runtime already abstracts providers behind ``create_llm_with_error_handling``
(see ``llm_utils.py``), but every call in the OODA loop uses the *same* global
``LLM_PROVIDER`` and model. The workload, however, is heterogeneous:

- Supervisor **routing** and **narration** are cheap, high-frequency calls where
  a small/fast model is fine.
- The Reflector's **hypothesis** and the Planner's **remediation plan** are the
  high-stakes reasoning calls that justify a stronger (and pricier) model.

The *hypothesis* is that routing each task to an appropriate model tier cuts
cost and latency without sacrificing quality on the calls that matter. It is
plausible and it is unmeasured — see "Scope, stated plainly" above. This
router makes the decision explicit, deterministic, testable, and configurable
via environment variables; it does not make the hypothesis true, and no
document in this repository should assert the saving as a result. It is a
strict superset of the previous behaviour: when disabled
(``MODEL_ROUTER_ENABLED=false``) it falls back to the existing single-provider
path, so nothing breaks.

Design
------
``select_model()`` is pure logic (no LLM imports) so it is trivially unit-tested.
``route_llm()`` performs the same selection and then lazily delegates to the
existing provider constructors. Provider/model identity is explicit; Sentinel
does not silently cross a tenant's configured provider boundary.
"""

from __future__ import annotations

import logging
import os
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Per-cluster decrypted LLM API key, bound for the duration of one graph
# execution (see agent_runtime.py). Task-local via contextvars rather than
# threaded as a plain argument/kwarg: route_llm() is called from deep inside
# LangGraph node closures that only carry AgentState (which the checkpointer
# persists to Postgres/Redis) — a real API key must never end up in that
# state, so it travels out-of-band instead.
_current_api_key: ContextVar[Optional[str]] = ContextVar(
    "_current_api_key", default=None
)


def bind_api_key(api_key: Optional[str]):
    """Bind the current task's LLM API key; returns a token for reset_api_key()."""
    return _current_api_key.set(api_key)


def reset_api_key(token) -> None:
    _current_api_key.reset(token)


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
    """Per-request signals the router can use beyond task type.

    Available to callers, exercised by tests, and supplied by **no production
    call site** — `route_llm` is always invoked without it, so budget downgrade
    and off-policy blocking never fire in a live run. Kept rather than deleted
    because the blocking semantics are the right shape for a budget owner to
    wire up; do not describe them as active until one does.
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


def _router_enabled(override: Optional[bool] = None) -> bool:
    if override is not None:
        return override
    return os.getenv("MODEL_ROUTER_ENABLED", "true").lower() in ("true", "1", "yes")


# Fallback tier defaults for Anthropic — used only when the caller has no
# per-cluster anchor model to route relative to (e.g. a "local" execution
# context, or a model the ladder below doesn't recognize at all).
_ANTHROPIC_TIER_DEFAULTS: Dict[ModelTier, str] = {
    ModelTier.FAST: "claude-haiku-4-5-20251001",
    ModelTier.BALANCED: "claude-sonnet-5",
    ModelTier.STRONG: "claude-opus-5",
}

# Anthropic's model families ordered cheapest/fastest -> strongest/priciest.
# Escalating/downgrading a tier moves ONE rung from the cluster's own chosen
# model (the "anchor") instead of jumping straight to a fixed absolute model
# — e.g. an anchor of claude-sonnet-4-5 escalates to claude-sonnet-5, not all
# the way to claude-opus-5, when a task actually needs the strong tier. This
# keeps cost proportional to how much stronger a task really needs to be, not
# to how strong the platform's single hardcoded "best" model happens to be.
_ANTHROPIC_LADDER: List[str] = [
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-5",
    "claude-sonnet-5",
    "claude-opus-5",
]

_FAMILY_RANK = {"haiku": 0, "sonnet": 1, "opus": 2}


def _normalize_anthropic_model(model_id: str) -> str:
    """Strip a litellm provider prefix, trailing date stamp, and "-latest"."""
    bare = model_id.rsplit("/", 1)[-1]
    parts = bare.split("-")
    if parts and parts[-1] == "latest":
        parts = parts[:-1]
    if parts and parts[-1].isdigit() and len(parts[-1]) >= 6:
        parts = parts[:-1]
    return "-".join(parts)


def _model_family_and_version(model_id: str) -> Optional[tuple]:
    """Parse (family_rank, version_number) from a model id, or None if unrecognized.

    The version number treats up-to-2-digit numeric tokens in the id as
    decimal places, e.g. "sonnet-4-5" -> 4.5, "sonnet-5" -> 5.0, so ordering
    stays correct across both hyphenated ("4-5") and un-hyphenated future
    naming without needing every real model id hardcoded.
    """
    bare = _normalize_anthropic_model(model_id)
    family = next((name for name in _FAMILY_RANK if name in bare), None)
    if family is None:
        return None
    digits = [tok for tok in bare.split("-") if tok.isdigit() and len(tok) <= 2]
    version = float(".".join(digits)) if digits else 0.0
    return (_FAMILY_RANK[family], version)


def _ladder_index(model_id: str) -> Optional[int]:
    """Position of ``model_id`` in ``_ANTHROPIC_LADDER``, exact or nearest-match."""
    bare = _normalize_anthropic_model(model_id)
    for i, candidate in enumerate(_ANTHROPIC_LADDER):
        if _normalize_anthropic_model(candidate) == bare:
            return i

    parsed = _model_family_and_version(model_id)
    if parsed is None:
        return None
    # Nearest ladder entry by (family_rank, version) distance; ties favor the
    # lower/cheaper index so an unfamiliar model doesn't over-escalate.
    best_i, best_distance = None, None
    for i, candidate in enumerate(_ANTHROPIC_LADDER):
        lp = _model_family_and_version(candidate)
        if lp is None:
            continue
        distance = abs(lp[0] - parsed[0]) * 100 + abs(lp[1] - parsed[1])
        if best_distance is None or distance < best_distance:
            best_i, best_distance = i, distance
    return best_i


def _anthropic_tier_model(tier: ModelTier, anchor_model: Optional[str]) -> Optional[str]:
    """Resolve a tier to a model, relative to the cluster's own chosen model.

    BALANCED always returns the anchor verbatim (never substitute the user's
    own pick). FAST/STRONG step one rung down/up the ladder from the anchor's
    position, clamped at the ladder's ends so an already-cheapest or already-
    strongest anchor doesn't get a pointless "escalation"/"downgrade" that
    doesn't actually change anything. Falls back to the fixed defaults only
    when there's no anchor or the anchor isn't recognized at all.
    """
    if not anchor_model:
        return _ANTHROPIC_TIER_DEFAULTS.get(tier)

    if tier == ModelTier.BALANCED:
        return anchor_model

    idx = _ladder_index(anchor_model)
    if idx is None:
        return _ANTHROPIC_TIER_DEFAULTS.get(tier)

    if tier == ModelTier.STRONG:
        return _ANTHROPIC_LADDER[min(idx + 1, len(_ANTHROPIC_LADDER) - 1)]
    return _ANTHROPIC_LADDER[max(idx - 1, 0)]


def _default_provider() -> str:
    return os.getenv("LLM_PROVIDER", "anthropic")


def _escalate(tier: ModelTier, steps: int = 1) -> ModelTier:
    """Bump a tier up by ``steps`` positions, clamped at STRONG."""
    idx = min(_TIER_ORDER.index(tier) + steps, len(_TIER_ORDER) - 1)
    return _TIER_ORDER[idx]


def _downgrade(tier: ModelTier, steps: int = 1) -> ModelTier:
    """Bump a tier down by ``steps`` positions, clamped at FAST."""
    idx = max(_TIER_ORDER.index(tier) - steps, 0)
    return _TIER_ORDER[idx]


def _tier_provider(tier: ModelTier, default_provider: str) -> str:
    """Provider for a tier, validated against the providers the runtime has.

    The per-tier override exists so a tier can be pinned somewhere other than
    ``LLM_PROVIDER``, but it is checked against ``SUPPORTED_PROVIDERS`` — which
    is ``("anthropic",)`` — instead of being trusted.

    Before this check the override was a hole in the fail-closed provider
    contract: ``provider_config`` refuses ``LLM_PROVIDER=groq`` at startup with
    a migration message, while ``MODEL_ROUTER_STRONG_PROVIDER=groq`` sailed past
    it and only came apart later, inside a graph node, on the one call that
    happened to route to the strong tier. Same rejection, same message, at the
    routing boundary now.
    """
    override = os.getenv(f"MODEL_ROUTER_{tier.value.upper()}_PROVIDER", "").strip()
    if not override:
        return default_provider

    from .provider_config import require_supported_provider

    return require_supported_provider(override)


def _tier_model_override(
    tier: ModelTier, provider: str, anchor_model: Optional[str] = None
) -> Optional[str]:
    """Explicit model id for a (tier, provider), if configured.

    Checked most-specific first so you can pin a model per provider *and* tier::

        MODEL_ROUTER_STRONG_MODEL_NVIDIA=meta/llama-3.3-70b-instruct
        MODEL_ROUTER_FAST_MODEL=llama-3.1-8b-instant

    An explicit env override always wins (operator opt-in). Absent that, for
    Anthropic the tier is resolved relative to ``anchor_model`` (the cluster's
    own chosen model) via ``_anthropic_tier_model`` — see that function for
    why this doesn't just jump to the strongest model available.
    """
    specific = os.getenv(f"MODEL_ROUTER_{tier.value.upper()}_MODEL_{provider.upper()}")
    if specific:
        return specific
    generic = os.getenv(f"MODEL_ROUTER_{tier.value.upper()}_MODEL")
    if generic:
        return generic
    if provider == "anthropic":
        return _anthropic_tier_model(tier, anchor_model)
    return None


def select_model(
    task_type: TaskType,
    complexity: str = "simple",
    provider: Optional[str] = None,
    policy: Optional[Dict[TaskType, ModelTier]] = None,
    request: Optional[RequestContext] = None,
    router_enabled: Optional[bool] = None,
    anchor_model: Optional[str] = None,
) -> RoutingDecision:
    """Decide which model tier / provider / model a task should use.

    Pure function — no LLM libraries imported — so it is cheap and easy to test.

    Three axes are implemented; only the first is fed by production callers:
    1. **Task complexity** — task type + simple/complex escalate the tier.
       Live callers pass the task type and leave ``complexity`` at "simple".
    2. **Budget** — a low remaining budget downgrades the tier (cheaper model);
       an exhausted budget blocks the request. No live caller passes ``request``.
    3. **Policy** — an off-policy request is blocked outright. Same: unused live.

    Args:
        task_type: What the LLM call is for (see :class:`TaskType`).
        complexity: "simple" or "complex". "complex" bumps the task up one tier.
        provider: Base provider override; defaults to ``LLM_PROVIDER`` env.
        policy: Optional task→tier policy override (defaults to the built-in one).
        request: Optional per-request budget/policy signals (see :class:`RequestContext`).
        router_enabled: Per-cluster override for whether routing is active;
            defaults to the ``MODEL_ROUTER_ENABLED`` env var when ``None``.
        anchor_model: The cluster's own chosen model (e.g. from Settings). When
            given, FAST/STRONG tiers resolve to one rung below/above this model
            on the Anthropic ladder instead of a fixed absolute model — see
            ``_anthropic_tier_model``.

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
    if not _router_enabled(router_enabled):
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
    model_override = _tier_model_override(tier, tier_provider, anchor_model)

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
    router_enabled: Optional[bool] = None,
    anchor_model: Optional[str] = None,
    **kwargs,
):
    """Select a model for ``task_type`` and build the LLM instance.

    Delegates construction to the existing ``llm_utils`` helpers (imported lazily
    so importing this module has no heavy dependencies). ``use_fallback`` is a
    backward-compatible constructor selector; neither path silently changes the
    configured provider, so accounting records fallback as disallowed.

    Raises:
        ModelRouterBlocked: if the request is off-policy or the budget is exhausted.

    Returns:
        An LLM instance, exactly as ``create_llm_with_error_handling`` would.
    """
    decision = select_model(
        task_type,
        complexity=complexity,
        provider=provider,
        request=request,
        router_enabled=router_enabled,
        anchor_model=anchor_model,
    )
    if decision.blocked:
        logger.warning(f"ModelRouter BLOCKED: {decision.block_reason}")
        raise ModelRouterBlocked(decision.block_reason)
    logger.info(f"ModelRouter: {decision.reason}")

    def account(llm):
        from .model_accounting import instrument_llm

        return instrument_llm(
            llm,
            task_type=decision.task_type.value,
            tier=decision.tier.value,
            requested_provider=decision.provider,
            requested_model=decision.model_id,
            fallback_allowed=False,
        )

    # LiteLLM backend (optional): our tier decides one explicit model and
    # LiteLLM provides the compatible transport. Falls through to the provider
    # path if not enabled, no tier model is configured, or LiteLLM is unavailable.
    from .litellm_backend import build_litellm_llm, litellm_enabled, tier_litellm_model

    if litellm_enabled():
        model = tier_litellm_model(
            decision.tier.value, provider=decision.provider, model_id=decision.model_id
        )
        if model:
            try:
                return account(
                    build_litellm_llm(
                        model,
                        temperature=decision.temperature,
                        max_tokens=kwargs.get("max_tokens"),
                        api_key=kwargs.get("api_key") or _current_api_key.get(),
                    )
                )
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
        return account(
            create_llm_with_fallback(
                primary_provider=decision.provider, **llm_kwargs
            )
        )
    return account(create_llm_with_error_handling(decision.provider, **llm_kwargs))


# --- Anthropic prompt caching -------------------------------------------------
#
# ``langchain_anthropic`` ships an official ``AnthropicPromptCachingMiddleware``,
# but it targets ``langchain.agents.create_agent`` (LangChain v1's agent
# framework). This codebase builds agents with ``langgraph.prebuilt.
# create_react_agent`` and plain ``.ainvoke()`` calls, neither of which accepts
# middleware, so we replicate the middleware's tagging logic by hand: tag the
# last content block of a static system prompt, and the last tool in a tool
# list, with Anthropic's ``cache_control`` marker. Anthropic caches everything
# up to and including a tagged block, so a single trailing marker is enough to
# cover an entire (unchanging) system prompt or tool catalog. Sub-1024-token
# prompts silently skip caching (no error, no extra cost), so it's always safe
# to tag a block whether or not it will actually reach the cacheable minimum.


def _prompt_cache_enabled() -> bool:
    return os.getenv("ANTHROPIC_PROMPT_CACHE_ENABLED", "true").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _prompt_cache_ttl() -> str:
    ttl = os.getenv("ANTHROPIC_PROMPT_CACHE_TTL", "1h").strip().lower()
    return ttl if ttl in ("5m", "1h") else "1h"


def cache_control_marker() -> Optional[Dict[str, str]]:
    """The ``cache_control`` value to tag a static block with, or ``None`` when
    prompt caching is disabled (``ANTHROPIC_PROMPT_CACHE_ENABLED=false``)."""
    if not _prompt_cache_enabled():
        return None
    return {"type": "ephemeral", "ttl": _prompt_cache_ttl()}


def cached_system_message(content: str):
    """Build a ``SystemMessage`` whose content is tagged for Anthropic prompt
    caching. Only worth calling with prompt text that is identical across
    calls (a static instruction block) — dynamic, per-incident content should
    stay out of the tagged block or the cache will never hit."""
    from langchain_core.messages import SystemMessage

    marker = cache_control_marker()
    if not marker or not content:
        return SystemMessage(content=content)
    return SystemMessage(content=[{"type": "text", "text": content, "cache_control": marker}])


def cached_tools(tools: List) -> List:
    """Return ``tools`` with the last entry tagged for Anthropic prompt
    caching, so the whole tool-definition block (sent as one contiguous
    span) is cached alongside the system prompt. No-op if caching is
    disabled, the list is empty, or the last entry isn't a ``BaseTool``."""
    marker = cache_control_marker()
    if not marker or not tools:
        return tools
    from langchain_core.tools import BaseTool

    last = tools[-1]
    if not isinstance(last, BaseTool):
        return tools
    new_extras = {**(getattr(last, "extras", None) or {}), "cache_control": marker}
    return [*tools[:-1], last.model_copy(update={"extras": new_extras})]
