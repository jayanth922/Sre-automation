#!/usr/bin/env python3
"""Ablation arms: configurations that remove one architectural component.

Sentinel claims that a supervisor-routed specialist split, a reflector, and
learned memory each earn their complexity. Architecture diagrams cannot settle
that; only removing a component and re-measuring can. An arm is therefore a
*measurement* configuration, not a deployment mode: it names exactly one thing
to take away so the difference can be attributed.

Two properties make the resulting evidence trustworthy, and both live here
rather than in the harness that reads the results:

1. The arm is part of the A01 run manifest's ``runtime`` section, which is one
   of the sections `configuration_fingerprint()` hashes. Two arms therefore
   cannot share a configuration fingerprint, and the paired evaluator already
   refuses to compare trials that do. Nobody has to remember to set a
   different ``BENCH_CONFIG_FINGERPRINT`` by hand.

2. While an experiment is active, *no* arm writes learned memory — not even
   the control. The arms run sequentially against the same cluster, so a full
   run that stored a new skill would hand the arm that runs after it a corpus
   the earlier arm never had, and the comparison would silently measure
   run order instead of architecture.

``SENTINEL_ABLATION_ARM`` unset is ordinary production: the full architecture,
learning enabled. Setting it — even to ``full`` — declares an experiment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Mapping, Optional

ENV_VAR = "SENTINEL_ABLATION_ARM"
FULL_ARM = "full"


class AblationError(RuntimeError):
    """The requested ablation arm is not one this build knows how to run."""


@dataclass(frozen=True)
class AblationArm:
    """One architectural configuration under measurement.

    Each flag is a component that is present (True) or removed (False). An arm
    removes at most one of them: removing two makes the difference
    unattributable, which is the whole failure the ablation exists to avoid.
    """

    name: str
    multi_agent: bool
    reflector: bool
    learned_memory: bool
    summary: str

    @property
    def is_full(self) -> bool:
        return self.name == FULL_ARM

    @property
    def removed(self) -> tuple[str, ...]:
        return tuple(
            component
            for component, present in (
                ("multi_agent", self.multi_agent),
                ("reflector", self.reflector),
                ("learned_memory", self.learned_memory),
            )
            if not present
        )


ARMS: Mapping[str, AblationArm] = {
    FULL_ARM: AblationArm(
        name=FULL_ARM,
        multi_agent=True,
        reflector=True,
        learned_memory=True,
        summary="The shipped architecture, unchanged. The control arm.",
    ),
    "single_agent": AblationArm(
        name="single_agent",
        multi_agent=False,
        reflector=True,
        learned_memory=True,
        summary=(
            "One ReAct loop holding the union of every specialist's tools. No "
            "supervisor routing, no handoffs, no per-specialist isolation. "
            "Same model, same tools, same scenarios."
        ),
    ),
    "no_reflector": AblationArm(
        name="no_reflector",
        multi_agent=True,
        reflector=False,
        learned_memory=True,
        summary=(
            "Investigation goes straight from the supervisor's terminal "
            "decision to the planner. No ORIENT stage and no bounded "
            "re-investigation loop."
        ),
    ),
    "no_memory": AblationArm(
        name="no_memory",
        multi_agent=True,
        reflector=True,
        learned_memory=False,
        summary=(
            "Incident memory and verified skills are neither read nor "
            "written. Static runbooks remain: they are authored knowledge, "
            "not something the system learned."
        ),
    ),
}


@dataclass(frozen=True)
class AblationConfig:
    """The arm in force, and whether an experiment declared it."""

    arm: AblationArm
    experiment_active: bool

    @property
    def multi_agent(self) -> bool:
        return self.arm.multi_agent

    @property
    def reflector(self) -> bool:
        return self.arm.reflector

    @property
    def reads_learned_memory(self) -> bool:
        return self.arm.learned_memory

    @property
    def writes_learned_memory(self) -> bool:
        """Frozen for every arm during an experiment — see the module docstring."""
        return self.arm.learned_memory and not self.experiment_active

    def manifest_entry(self) -> dict[str, object]:
        """What the A01 ``runtime`` section records, and therefore fingerprints."""
        return {
            "ablation_arm": self.arm.name,
            "ablation_experiment": self.experiment_active,
            "learned_memory_writes": self.writes_learned_memory,
        }

    def describe(self) -> str:
        if not self.experiment_active:
            return "production (full architecture, learning enabled)"
        removed = ", ".join(self.arm.removed) or "nothing"
        return f"ablation arm {self.arm.name} (removed: {removed}, learning frozen)"


PRODUCTION = AblationConfig(arm=ARMS[FULL_ARM], experiment_active=False)


def resolve_arm(raw: Optional[str]) -> AblationConfig:
    """Parse a raw arm name. Fails closed: an unknown arm never degrades to full.

    A typo that silently ran the control arm would publish a comparison of the
    full system against itself and report — truthfully, and uselessly — no
    difference.
    """
    if raw is None:
        return PRODUCTION
    name = raw.strip().lower()
    if not name:
        return PRODUCTION
    arm = ARMS.get(name)
    if arm is None:
        known = ", ".join(sorted(ARMS))
        raise AblationError(f"unknown ablation arm {name!r}; expected one of: {known}")
    return AblationConfig(arm=arm, experiment_active=True)


@lru_cache(maxsize=8)
def _resolve_cached(raw: Optional[str]) -> AblationConfig:
    return resolve_arm(raw)


def current_ablation() -> AblationConfig:
    """The arm this process is running, read from the environment."""
    return _resolve_cached(os.getenv(ENV_VAR))
