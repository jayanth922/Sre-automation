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
    reflection_max_output_tokens: int
    planning_max_output_tokens: int
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
        #
        # 3000 was those percentiles plus headroom, and it was measured on
        # *text*. The balanced tier is an extended-thinking model (see
        # litellm_backend._FIXED_TEMPERATURE, which forces temperature=1 for
        # exactly that reason), and thinking is billed and capped out of the
        # same allowance. On 2026-09-23 the metrics lane spent its whole
        # allowance thinking twice over -- 31.7s and 34.4s, the two longest
        # model calls of the run -- and emitted no text and no tool call
        # either time. Both turns were billed in full and returned nothing.
        #
        # 4096 was the next step and it was still short: across the five
        # traced runs of 2026-09-30 some lane hit exactly 4096 in every run
        # (1-3 turns each, p90 up to 4096), and in Run 8 the Kubernetes and
        # Runbooks lanes were cut off mid-report after their tool rounds.
        # context_compaction reserves the largest configured ceiling out of
        # every input budget, so raising this moves the reservation with it.
        # A ceiling is not a spend -- output is billed as generated.
        specialist_max_output_tokens=_bounded_int(
            "SPECIALIST_MAX_OUTPUT_TOKENS", 8192, minimum=256, maximum=16000
        ),
        # The reflection is the one call that writes the whole structured
        # diagnosis -- hypothesis, causal chain, evidence, unknowns and
        # reasoning in a single tool call -- and it inherited the 4096
        # default meant for a chat turn. Every reflection of E2E Runs 4-6
        # (five of five) stopped at exactly 4096 output tokens, cut off after
        # causal_chain: evidence and unknowns parsed as their empty defaults,
        # which failed evidence_support and emptied the derived timeline.
        # A ceiling is not a spend; output is billed as generated.
        reflection_max_output_tokens=_bounded_int(
            "REFLECTION_MAX_OUTPUT_TOKENS", 12000, minimum=1024, maximum=32000
        ),
        # Both planning calls -- the supervisor's investigation plan and the
        # remediation plan -- write one structured tool call and inherited
        # the 4096 chat default. The remediation plan used 2865-3300 of it in
        # all five traced runs of 2026-09-30; a plan cut off there parses as
        # a failure and falls back to escalation. Same lesson as reflection.
        planning_max_output_tokens=_bounded_int(
            "PLANNING_MAX_OUTPUT_TOKENS", 8192, minimum=1024, maximum=32000
        ),
        reinvestigation_rounds=_bounded_int(
            "MAX_INVESTIGATION_DEPTH", 1, minimum=0, maximum=3
        ),
    )
