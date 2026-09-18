#!/usr/bin/env python3
"""Source contract: shipped config advertises only providers the runtime accepts.

`provider_config.SUPPORTED_PROVIDERS` is `("anthropic",)` and the check runs
before the API serves, so a chart value, ConfigMap, or example Secret naming
gemini/groq/nvidia is not an option an operator can take — it is a guaranteed
CrashLoopBackOff with a migration message. These tests keep the deployment
surface and the allow-list from drifting apart again.

Prose that *names* a removed provider is fine (the migration messages have to
say what was removed); what is checked here is configuration — env-var keys and
provider values that an operator would actually set.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from sre_agent.provider_config import SUPPORTED_PROVIDERS, ProviderConfigError

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "deploy" / "helm" / "sentinel"

# Credential/model env keys for providers the runtime refuses. Their presence in
# a shipped file means we are asking operators to fill in a key we will reject.
DEAD_PROVIDER_KEYS = (
    "GROQ_API_KEY",
    "GROQ_MODEL",
    "GOOGLE_API_KEY",
    "GEMINI_MODEL",
    "NVIDIA_API_KEY",
    "NVIDIA_MODEL",
    "OPENAI_MODEL",
)

# Files an operator copies or edits to configure a deployment.
CONFIG_FILES = (
    ROOT / ".env.example",
    ROOT / "deploy" / "k8s" / "config.yaml",
    ROOT / "deploy" / "k8s" / "secret.example.yaml",
    ROOT / "deploy" / "terraform" / "secret.example.yaml",
    CHART / "values.yaml",
    CHART / "templates" / "configmap.yaml",
    CHART / "templates" / "secret.yaml",
)

# `KEY: "value"` (YAML) or `KEY=value` (dotenv), ignoring commented lines.
_ASSIGNMENT = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*[:=]")


def _assigned_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        match = _ASSIGNMENT.match(line)
        if match:
            keys.add(match.group(1))
    return keys


@pytest.mark.parametrize("path", CONFIG_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_config_files_set_no_dead_provider_keys(path: Path):
    """No shipped config asks for a credential the runtime will refuse."""
    assert path.exists(), f"{path} moved; update this contract test"
    offenders = sorted(_assigned_keys(path) & set(DEAD_PROVIDER_KEYS))
    assert not offenders, (
        f"{path.relative_to(ROOT)} still configures {offenders}; "
        f"supported providers are {SUPPORTED_PROVIDERS}"
    )


def test_shipped_provider_values_are_supported():
    """Every concrete provider value in shipped config is on the allow-list."""
    patterns = (
        re.compile(r'^\s*LLM_PROVIDER\s*[:=]\s*["\']?([a-z_]+)', re.MULTILINE),
        re.compile(r'^\s*provider\s*:\s*["\']?([a-z_]+)', re.MULTILINE),
    )
    for path in CONFIG_FILES:
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            for value in pattern.findall(text):
                assert value in SUPPORTED_PROVIDERS, (
                    f"{path.relative_to(ROOT)} sets provider={value!r}, "
                    f"which fails startup; supported: {SUPPORTED_PROVIDERS}"
                )


def test_chart_templates_only_reference_defined_values():
    """Every `.Values.<x>` a template reads is declared in values.yaml.

    Deleting the dead `secrets.groqApiKey` / `llm.groqModel` entries from
    values.yaml left `secret.yaml` and `configmap.yaml` referencing keys that no
    longer existed. Helm renders those as empty strings rather than failing, so
    the chart stayed installable while quietly shipping blank config — exactly
    the kind of silent drift this file exists to stop.
    """
    yaml = pytest.importorskip("yaml")
    values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))

    def defined(dotted: str) -> bool:
        node = values
        for segment in dotted.split("."):
            if not isinstance(node, dict) or segment not in node:
                return False
            node = node[segment]
        return True

    reference = re.compile(r"\.Values\.([A-Za-z_][A-Za-z0-9_.]*[A-Za-z0-9_])")
    missing: list[str] = []
    for template in sorted((CHART / "templates").iterdir()):
        if not template.is_file():
            continue
        text = template.read_text(encoding="utf-8")
        for dotted in sorted(set(reference.findall(text))):
            if not defined(dotted):
                missing.append(f"{template.name}: .Values.{dotted}")

    assert not missing, "chart templates read undeclared values: " + "; ".join(missing)


def test_chart_refuses_unsupported_provider_at_render_time():
    """The chart fails the install, not the pod, on a bad provider."""
    validate = (CHART / "templates" / "validate.yaml").read_text(encoding="utf-8")
    assert 'ne .Values.llm.provider "anthropic"' in validate
    assert "fail" in validate


def test_tier_provider_override_is_validated(monkeypatch):
    """`MODEL_ROUTER_<TIER>_PROVIDER` obeys the same allow-list as LLM_PROVIDER.

    This override used to bypass the startup check entirely: the process came up
    healthy and only came apart later, inside a graph node, on the first call
    routed to that tier.
    """
    from sre_agent import model_router

    monkeypatch.setenv("MODEL_ROUTER_STRONG_PROVIDER", "groq")
    with pytest.raises(ProviderConfigError):
        model_router._tier_provider(model_router.ModelTier.STRONG, "anthropic")

    monkeypatch.setenv("MODEL_ROUTER_STRONG_PROVIDER", "anthropic")
    assert (
        model_router._tier_provider(model_router.ModelTier.STRONG, "anthropic")
        == "anthropic"
    )

    monkeypatch.delenv("MODEL_ROUTER_STRONG_PROVIDER", raising=False)
    assert (
        model_router._tier_provider(model_router.ModelTier.STRONG, "anthropic")
        == "anthropic"
    )


def test_settings_carry_no_dead_provider_fields():
    """Settings stopped loading keys for providers the runtime refuses."""
    from sre_agent.config import Settings

    fields = set(Settings.model_fields)
    assert "anthropic_api_key" in fields
    assert not fields & {"groq_api_key", "google_api_key", "nvidia_api_key"}


def test_anthropic_model_defaults_stay_on_the_router_ladder():
    """An off-ladder anchor silently disables per-tier escalation."""
    from sre_agent.model_router import _ANTHROPIC_LADDER

    ladder = set(_ANTHROPIC_LADDER)
    shipped = {
        ROOT / "deploy" / "k8s" / "config.yaml": r'ANTHROPIC_MODEL:\s*"([^"]+)"',
        ROOT / ".env.example": r'ANTHROPIC_MODEL="([^"]+)"',
        CHART / "values.yaml": r'anthropicModel:\s*"([^"]+)"',
    }
    for path, pattern in shipped.items():
        found = re.findall(pattern, path.read_text(encoding="utf-8"))
        assert found, f"{path.relative_to(ROOT)} no longer pins an Anthropic model"
        for model in found:
            assert model in ladder, (
                f"{path.relative_to(ROOT)} anchors {model!r}, which is off the "
                f"router ladder {sorted(ladder)} — per-tier escalation would "
                "silently fall back to fixed defaults"
            )


def test_no_provider_file_is_skipped_by_accident():
    """Guard the guard: the config file list must not silently go stale."""
    for path in CONFIG_FILES:
        assert path.exists(), f"{path.relative_to(ROOT)} is gone; fix CONFIG_FILES"
    assert os.path.isdir(CHART / "templates")
