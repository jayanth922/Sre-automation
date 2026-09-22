"""Deterministic limits for the model-driven investigation loop.

Cost is reported only after a provider call completes, so a dollar threshold
cannot be a hard pre-call boundary. These limits constrain the two dimensions
that are knowable before spending: model turns inside each specialist and the
number of reflector-directed reinvestigation rounds.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass


def _bounded_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


@dataclass(frozen=True)
class InvestigationLimits:
    """Code-owned limits that are also pinned in the run manifest."""

    specialist_model_turns: int
    specialist_timeout_seconds: int
    reinvestigation_rounds: int

    def manifest_entry(self) -> dict[str, int]:
        return asdict(self)


def investigation_limits() -> InvestigationLimits:
    return InvestigationLimits(
        specialist_model_turns=_bounded_int(
            "SPECIALIST_MAX_MODEL_TURNS", 6, minimum=1, maximum=20
        ),
        specialist_timeout_seconds=_bounded_int(
            "SPECIALIST_TIMEOUT_SECONDS", 120, minimum=15, maximum=300
        ),
        reinvestigation_rounds=_bounded_int(
            "MAX_INVESTIGATION_DEPTH", 1, minimum=0, maximum=3
        ),
    )
