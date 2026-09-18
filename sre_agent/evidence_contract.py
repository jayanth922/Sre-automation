"""One typed contract for measured evidence, from tool result to severity gate.

Severity is the one decision in this system that a model must never make by
narration, so every number feeding it has to be traceable to a tool that
actually returned it. That principle was already enforced — `act_phase` reads
tool results and never an `AIMessage` — but it was enforced three times, in
three vocabularies:

* `_walk_metrics` held the set of metric names worth collecting;
* `_absorb` held the rule for what type each of those names coerces to;
* `extract_incident_signals` held that same type rule again, inline, for the
  alert-label path.

Three copies of one definition drift. Adding a metric to the walker without
teaching `_absorb` its type silently discards it; the walker and the label
reader could disagree about whether `affected_pods` is an int, and nothing
would say so. This module is the single definition all three now import.

The second problem it solves is a boundary. Measured evidence does not travel
from producer to consumer in memory: `agent_nodes` projects it into
``metadata["measured_evidence"]`` so the full specialist transcript can be
offloaded to a durable artifact (see `evidence_artifacts.py`), and the severity
gate reads it back later, possibly in another process and after a checkpoint
restore. Across that gap it was a bare nested dict that anything could write
and that the reader re-validated field by field with `isinstance` checks. A
record now has to parse to be believed, and a record that will not parse is
dropped loudly instead of half-read.

Related but distinct shapes, deliberately not merged:

* `EvidenceLink` (`severity_engine.py`) is what the *gate* records — the
  feature it used, with provenance, including "this was unknown". An
  `EvidenceRecord` is an observation; an `EvidenceLink` is a decision input.
  One record can supersede another and lose its link (`_absorb`).
* `EvidenceReference` (`agent_state.py`) is a citation the model writes to
  support a prose claim. It is model-authored, so it is exactly what severity
  must not consume.
* `evidence_artifacts.py` moves whole transcripts into content-addressed
  storage. It is the reason this boundary exists, not a participant in it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

__all__ = [
    "ALIASES_OF",
    "EvidenceContractError",
    "EvidenceRecord",
    "METRIC_ALIASES",
    "METRIC_NAMES",
    "SEVERITY_METRICS",
    "canonical_metric",
    "coerce_metric",
]


class EvidenceContractError(ValueError):
    """A measured-evidence record is malformed and cannot be trusted."""


def _as_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> Optional[int]:
    number = _as_float(value)
    if number is None:
        return None
    return int(number)


def _as_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y"}:
        return True
    if text in {"0", "false", "no", "n"}:
        return False
    return None


def _as_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# Every metric the severity gate will accept, and the only place its type is
# declared. A metric absent here is not evidence: the walker will not collect
# it and the gate will not coerce it.
SEVERITY_METRICS: Mapping[str, Any] = {
    "error_rate": _as_float,
    "slo_burn_rate": _as_float,
    "saturation": _as_float,
    "error_rate_slope": _as_float,
    "duration_seconds": _as_float,
    "affected_pods": _as_int,
    "affected_services": _as_int,
    "dependency_count": _as_int,
    "slo_breached": _as_bool,
    "still_escalating": _as_bool,
    "customer_scope": _as_str,
}

# Spellings a tool or alert may use for a metric that already has a canonical
# name. Resolved on the way in so the gate only ever sees canonical names.
METRIC_ALIASES: Mapping[str, str] = {
    "burn_rate": "slo_burn_rate",
}

# What a walker should look for: canonical names plus accepted aliases.
METRIC_NAMES = frozenset(SEVERITY_METRICS) | frozenset(METRIC_ALIASES)

# The reverse view, for readers that must probe every accepted spelling of a
# metric (alert labels and annotations, which Sentinel does not control).
ALIASES_OF: Mapping[str, tuple] = {
    canonical: tuple(
        alias for alias, target in METRIC_ALIASES.items() if target == canonical
    )
    for canonical in SEVERITY_METRICS
}


def canonical_metric(name: Any) -> Optional[str]:
    """Resolve a metric spelling to its canonical name, or None if unknown."""
    text = str(name).strip()
    if text in SEVERITY_METRICS:
        return text
    return METRIC_ALIASES.get(text)


def coerce_metric(metric: str, value: Any) -> Any:
    """Coerce a raw observation to the declared type for `metric`.

    Returns None when the metric is unknown or the value will not convert.
    None means "no usable measurement", which the severity engine reads as
    UNKNOWN — never as a calm zero.
    """
    canonical = canonical_metric(metric)
    if canonical is None:
        return None
    return SEVERITY_METRICS[canonical](value)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class EvidenceRecord:
    """One measured value, with the tool call that produced it.

    `agent`, `tool` and `pointer` are kept apart rather than pre-joined so the
    provenance stays queryable — "which tool produced every saturation reading
    in this incident" is a question the joined string cannot answer. `source`
    reassembles them in the `agent:tool:path` form the severity ledger and the
    operator-facing rationale already use.
    """

    metric: str
    value: Any
    agent: str
    tool: str
    pointer: str = ""
    observed_at: str = ""

    def __post_init__(self) -> None:
        canonical = canonical_metric(self.metric)
        if canonical is None:
            raise EvidenceContractError(f"unknown severity metric: {self.metric!r}")
        object.__setattr__(self, "metric", canonical)

        coerced = SEVERITY_METRICS[canonical](self.value)
        if coerced is None:
            raise EvidenceContractError(
                f"{canonical} value {self.value!r} is not a usable measurement"
            )
        object.__setattr__(self, "value", coerced)

        for field_name in ("agent", "tool"):
            text = str(getattr(self, field_name) or "").strip()
            if not text:
                # An unattributed number is indistinguishable from an invented
                # one, which is the entire failure this contract exists to stop.
                raise EvidenceContractError(f"{canonical} evidence has no {field_name}")
            object.__setattr__(self, field_name, text)

        object.__setattr__(self, "pointer", str(self.pointer or "").strip())
        object.__setattr__(
            self, "observed_at", str(self.observed_at or "").strip() or _now_iso()
        )

    @property
    def source(self) -> str:
        """Provenance in the `agent:tool:path` form the ledger records."""
        trail = f"{self.agent}:{self.tool}"
        return f"{trail}:{self.pointer}" if self.pointer else trail

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metric": self.metric,
            "value": self.value,
            "agent": self.agent,
            "tool": self.tool,
            "pointer": self.pointer,
            "observed_at": self.observed_at,
            # Denormalised on purpose: checkpoints written by this version are
            # read by older consumers that only know `value` and `source`.
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, metric: Any, payload: Any) -> "EvidenceRecord":
        """Parse one stored record, tolerating the pre-contract shape.

        Checkpoints written before this contract carry only `value` and a
        joined `source` string. Those are still real measurements from real
        tool calls, so they are split back apart rather than discarded — but a
        record with no provenance at all is rejected either way.
        """
        if not isinstance(payload, Mapping):
            raise EvidenceContractError(f"{metric!r} evidence is not a mapping")
        if "value" not in payload:
            raise EvidenceContractError(f"{metric!r} evidence has no value")

        agent = payload.get("agent")
        tool = payload.get("tool")
        pointer = payload.get("pointer", "")
        if not agent or not tool:
            agent, tool, pointer = _split_legacy_source(payload.get("source"))

        return cls(
            metric=payload.get("metric") or metric,
            value=payload["value"],
            agent=agent,
            tool=tool,
            pointer=pointer,
            observed_at=payload.get("observed_at", ""),
        )


def _split_legacy_source(source: Any) -> tuple[str, str, str]:
    """Recover `agent`, `tool` and pointer from a joined `agent:tool:path`."""
    text = str(source or "").strip()
    if not text:
        raise EvidenceContractError("evidence has no source")
    parts = text.split(":", 2)
    agent = parts[0].strip()
    tool = parts[1].strip() if len(parts) > 1 else ""
    pointer = parts[2].strip() if len(parts) > 2 else ""
    if not agent:
        raise EvidenceContractError("evidence has no source")
    # A pre-contract source could be a bare prefix such as `agent_results`,
    # with no tool segment. Keeping the prefix as both agent and tool is
    # honest: it says exactly as much as the old record actually knew.
    return agent, tool or agent, pointer
