"""Deterministic limits for the model-driven investigation loop.

Cost is reported only after a provider call completes, so a dollar threshold
cannot be a hard pre-call boundary. These limits constrain the two dimensions
that are knowable before spending: model turns inside each specialist, the
output tokens any one of those turns may emit, and the number of
reflector-directed reinvestigation rounds.
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
    specialist_max_output_tokens: int
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
        # A specialist turn is a report, not an essay. Across the 85
        # specialist turns of the graded inventory_slow_queries trial the
        # output length was p50 484, p75 857, p90 2,339 tokens -- and one
        # turn emitted 6,402, taking 68.6s of that lane's 120s budget at a
        # measured 89 tok/s. This bounds the tail without touching nine
        # turns in ten.
        specialist_max_output_tokens=_bounded_int(
            "SPECIALIST_MAX_OUTPUT_TOKENS", 3000, minimum=256, maximum=16000
        ),
        reinvestigation_rounds=_bounded_int(
            "MAX_INVESTIGATION_DEPTH", 1, minimum=0, maximum=3
        ),
    )
