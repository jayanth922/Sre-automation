#!/usr/bin/env python3
"""
Unit tests for the Model Router (project #6 integration).

These tests exercise the *decision* logic only (``select_model``), which is pure
Python with no LLM dependencies, so they run without the full runtime stack.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

# Import the module directly from its file path so the test does not require the
# rest of the sre_agent package (and its langchain deps) to be importable.
_MODULE_PATH = Path(__file__).resolve().parents[1] / "sre_agent" / "model_router.py"
_spec = importlib.util.spec_from_file_location("model_router", _MODULE_PATH)
model_router = importlib.util.module_from_spec(_spec)
# Register before exec so the module's dataclasses (with PEP 563 string
# annotations) can resolve their own types during class creation.
sys.modules[_spec.name] = model_router
_spec.loader.exec_module(model_router)

TaskType = model_router.TaskType
ModelTier = model_router.ModelTier
select_model = model_router.select_model
RequestContext = model_router.RequestContext
ModelRouterBlocked = model_router.ModelRouterBlocked


@pytest.fixture(autouse=True)
def _clean_router_env(monkeypatch):
    """Start each test from a known env: router on, ollama base, no overrides."""
    for key in list(os.environ):
        if key.startswith("MODEL_ROUTER_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MODEL_ROUTER_ENABLED", "true")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")


def test_high_stakes_tasks_route_to_strong():
    """Reflection and planning are the calls that justify the strong tier."""
    assert select_model(TaskType.REFLECTION).tier is ModelTier.STRONG
    assert select_model(TaskType.PLANNING).tier is ModelTier.STRONG


def test_cheap_tasks_route_to_fast():
    """Routing / narration / greeting are cheap, high-frequency calls."""
    for task in (TaskType.ROUTING, TaskType.NARRATION, TaskType.GREETING):
        assert select_model(task).tier is ModelTier.FAST


def test_specialist_and_aggregation_are_balanced():
    assert select_model(TaskType.SPECIALIST).tier is ModelTier.BALANCED
    assert select_model(TaskType.AGGREGATION).tier is ModelTier.BALANCED


def test_complexity_escalates_one_tier():
    """A 'complex' task bumps up exactly one tier, clamped at STRONG."""
    assert select_model(TaskType.SPECIALIST, complexity="simple").tier is ModelTier.BALANCED
    assert select_model(TaskType.SPECIALIST, complexity="complex").tier is ModelTier.STRONG
    # Already STRONG stays STRONG (clamp).
    assert select_model(TaskType.PLANNING, complexity="complex").tier is ModelTier.STRONG


def test_disabled_router_falls_back_to_balanced_base_provider(monkeypatch):
    """Disabled router reproduces the pre-router single-model behavior."""
    monkeypatch.setenv("MODEL_ROUTER_ENABLED", "false")
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    decision = select_model(TaskType.REFLECTION)
    assert decision.tier is ModelTier.BALANCED
    assert decision.provider == "groq"
    assert decision.model_id is None


def test_default_provider_from_env(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "nvidia")
    assert select_model(TaskType.SPECIALIST).provider == "nvidia"


def test_explicit_provider_overrides_env(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    assert select_model(TaskType.SPECIALIST, provider="groq").provider == "groq"


def test_per_tier_cross_provider_routing(monkeypatch):
    """Strong tier can be pinned to a different provider than the base."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("MODEL_ROUTER_STRONG_PROVIDER", "nvidia")
    decision = select_model(TaskType.PLANNING)  # planning → strong
    assert decision.provider == "nvidia"
    # Fast-tier tasks are unaffected and stay on the base provider.
    assert select_model(TaskType.ROUTING).provider == "ollama"


def test_per_tier_model_override(monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_STRONG_MODEL", "some-strong-model")
    assert select_model(TaskType.REFLECTION).model_id == "some-strong-model"


def test_provider_specific_model_override_wins(monkeypatch):
    """A (tier, provider)-specific override beats the generic tier override."""
    monkeypatch.setenv("LLM_PROVIDER", "nvidia")
    monkeypatch.setenv("MODEL_ROUTER_STRONG_MODEL", "generic-strong")
    monkeypatch.setenv("MODEL_ROUTER_STRONG_MODEL_NVIDIA", "nvidia-strong")
    assert select_model(TaskType.PLANNING).model_id == "nvidia-strong"


def test_string_task_type_is_accepted():
    """Callers may pass the raw string value instead of the enum."""
    assert select_model("reflection").tier is ModelTier.STRONG


def test_temperature_is_low_for_high_stakes_and_warmer_for_narration():
    assert select_model(TaskType.PLANNING).temperature <= 0.1
    assert select_model(TaskType.NARRATION).temperature > 0.1


# ── Budget + policy axes (transcript's precise #6 definition) ────────────────

def test_off_policy_request_is_blocked():
    d = select_model(TaskType.PLANNING, request=RequestContext(off_policy=True))
    assert d.blocked is True and "off-policy" in d.block_reason.lower()


def test_exhausted_budget_is_blocked():
    d = select_model(TaskType.PLANNING, request=RequestContext(remaining_budget=0))
    assert d.blocked is True and "budget" in d.block_reason.lower()


def test_low_budget_downgrades_tier():
    # PLANNING is normally STRONG; a low budget knocks it down a tier.
    normal = select_model(TaskType.PLANNING)
    low = select_model(TaskType.PLANNING, request=RequestContext(remaining_budget=0.5))
    assert normal.tier is ModelTier.STRONG
    assert low.tier is ModelTier.BALANCED
    assert low.blocked is False


def test_healthy_budget_does_not_downgrade():
    d = select_model(TaskType.PLANNING, request=RequestContext(remaining_budget=100.0))
    assert d.tier is ModelTier.STRONG


def test_no_request_context_is_unchanged():
    assert select_model(TaskType.PLANNING).tier is ModelTier.STRONG


def test_route_llm_raises_when_blocked():
    with pytest.raises(ModelRouterBlocked):
        model_router.route_llm(TaskType.PLANNING, request=RequestContext(off_policy=True))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
