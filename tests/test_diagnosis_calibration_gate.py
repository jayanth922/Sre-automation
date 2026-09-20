"""Severity may only trust a diagnosis artifact that certified an operating point.

`build_calibration_artifact` deliberately emits a *valid* artifact carrying
`autonomy_threshold=None` and an `autonomy_blocked_reason` whenever its corpus
is below `minimum_threshold_support` or was not observed live. That null
threshold is the artifact saying "I have a mapping, but not enough evidence for
it to decide anything". `calibrate_confidence` still returns a
`CalibratedConfidence` for such an artifact — it never returns None — so
testing `calibrated is not None` accepted exactly the artifacts that had
refused to certify themselves.

Two consequences, both fail-open, both covered here:

1. A below-support artifact set `hypothesis_confidence_calibrated=True`, which
   switched off "when the diagnosis is uncertain, round the severity up".
2. The mapping was computed and then thrown away: severity compared the
   *model's own self-report* against the round-up threshold, though
   `IncidentSignals.hypothesis_confidence` documents that only an empirically
   calibrated probability belongs there. A systematically overconfident model
   scored itself past the gate it was supposed to be measured against.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sre_agent.act_phase import extract_incident_signals  # noqa: E402
from sre_agent.confidence_calibration import (  # noqa: E402
    build_calibration_artifact,
    build_confidence_record,
    save_calibration_artifact,
)
from sre_agent.severity_engine import classify_severity  # noqa: E402

FINGERPRINT = "a" * 64
DATASET = "b" * 64
SOURCE = "c" * 64

# What the reflector claims about itself in every case below. Kept well above
# the 0.5 round-up threshold so the self-report alone would always clear it.
RAW = 0.9


@dataclass
class FakeAlert:
    severity: str = "warning"
    labels: Dict[str, str] = field(default_factory=lambda: {"service": "checkout"})
    alert_name: str = "HighErrorRate"


@dataclass
class FakeReflector:
    confidence: float = RAW


def _state() -> Dict[str, Any]:
    """Enough measured telemetry that severity reaches the round-up rule.

    Missing any of these short-circuits `classify_severity` to UNKNOWN before
    confidence is consulted, which would make every assertion below vacuous.
    """
    return {
        "alert_context": FakeAlert(),
        "remediation_plan": None,
        "reflector_analysis": FakeReflector(),
        "agent_results": {
            "MetricsAgent": {
                "findings": {
                    "error_rate": 0.02,
                    "slo_burn_rate": 0.5,
                    "slo_breached": False,
                    "saturation": 0.1,
                    "error_rate_slope": 0.0,
                    "still_escalating": False,
                    "affected_services": 1,
                }
            }
        },
        "incident_id": None,
        "metadata": {},
    }


def _corpus(*, overconfident: bool, count: int = 60) -> tuple:
    """A live-observed diagnosis corpus, either honest or systematically wrong.

    `overconfident` keeps every raw self-report high while only a fifth of the
    diagnoses were actually right — the shape that makes the difference between
    the raw number and the mapped one observable.
    """
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    records = []
    for index in range(count):
        if overconfident:
            raw = 0.80 + (index % 15) / 100.0
            outcome = index % 5 == 0
        elif index < count // 2:
            raw = 0.10 + (index % 20) / 100.0
            outcome = False
        else:
            raw = 0.80 + (index % 15) / 100.0
            outcome = True
        records.append(
            build_confidence_record(
                task="diagnosis",
                rubric_version="v2",
                raw_confidence=raw,
                outcome=outcome,
                scenario=f"scenario-{index % 7}",
                scenario_version="v2",
                dataset_sha256=DATASET,
                config_fingerprint=FINGERPRINT,
                pair_id=f"pair-{index}",
                observed_at=start + timedelta(minutes=index),
                evidence_source="live_benchmark",
            )
        )
    return tuple(records)


def _artifact(
    tmp_path: Path,
    records: tuple,
    *,
    name: str,
    minimum_threshold_support: int,
    required_wilson_lower: float,
):
    artifact = build_calibration_artifact(
        records,
        task="diagnosis",
        source_sha256=SOURCE,
        config_fingerprint=FINGERPRINT,
        artifact_version=name,
        minimum_samples=40,
        minimum_bin_samples=20,
        maximum_bins=2,
        minimum_threshold_support=minimum_threshold_support,
        required_wilson_lower=required_wilson_lower,
    )
    path = tmp_path / f"{name}.json"
    save_calibration_artifact(path, artifact)
    return artifact, path


def _blocked(tmp_path: Path):
    """Real evidence, honest mapping, but nowhere near enough of it to certify."""
    artifact, path = _artifact(
        tmp_path,
        _corpus(overconfident=False),
        name="blocked",
        minimum_threshold_support=1_000_000,
        required_wilson_lower=0.90,
    )
    assert artifact.autonomy_threshold is None
    assert artifact.autonomy_blocked_reason, "fixture must be a self-blocked artifact"
    return artifact, path


def _certified(tmp_path: Path, *, overconfident: bool):
    artifact, path = _artifact(
        tmp_path,
        _corpus(overconfident=overconfident),
        name="certified-overconfident" if overconfident else "certified",
        minimum_threshold_support=1,
        # An overconfident corpus cannot clear a real accuracy floor; the point
        # of that fixture is the mapping, not the operating point, so the floor
        # is dropped to let it certify anyway.
        required_wilson_lower=0.01 if overconfident else 0.50,
    )
    assert artifact.autonomy_threshold is not None, "fixture must certify"
    assert artifact.autonomy_blocked_reason is None
    return artifact, path


def _mapped(artifact) -> float:
    """The calibrated probability `RAW` maps to, by the artifact's own rule."""
    for item in artifact.bins:
        if RAW <= item.upper_bound:
            return item.calibrated_probability
    return artifact.bins[-1].calibrated_probability


def _signals(monkeypatch, path: Optional[Path]):
    if path is None:
        monkeypatch.delenv("DIAGNOSIS_CONFIDENCE_CALIBRATION_PATH", raising=False)
    else:
        monkeypatch.setenv("DIAGNOSIS_CONFIDENCE_CALIBRATION_PATH", str(path))
    monkeypatch.setenv("SENTINEL_CONFIG_FINGERPRINT", FINGERPRINT)
    return extract_incident_signals(_state())


def test_a_self_blocked_artifact_does_not_count_as_calibration(tmp_path, monkeypatch):
    """The regression: a null threshold must not read as "calibrated"."""
    _, path = _blocked(tmp_path)

    signals = _signals(monkeypatch, path)

    assert signals.hypothesis_confidence_calibrated is False
    # And the severity engine must therefore still round up, exactly as it does
    # when no artifact is configured at all.
    assessment = classify_severity(signals)
    assert assessment.rounded_up is True
    assert "uncalibrated" in assessment.rationale


def test_a_certified_artifact_suppresses_the_round_up(tmp_path, monkeypatch):
    """The other half: a real operating point must still be honoured."""
    artifact, path = _certified(tmp_path, overconfident=False)
    assert _mapped(artifact) >= 0.5, "fixture must map RAW above the round-up floor"

    signals = _signals(monkeypatch, path)

    assert signals.hypothesis_confidence_calibrated is True
    assert classify_severity(signals).rounded_up is False


def test_severity_reads_the_mapped_probability_not_the_self_report(
    tmp_path, monkeypatch
):
    """An overconfident model does not get to clear the gate on its own say-so.

    The artifact certifies, so the old code called this calibrated and then
    compared 0.9 — the model's opinion of itself — against 0.5 and let the
    severity stand. The empirical mapping says diagnoses that confident are
    right about a fifth of the time, so the incident must escalate.
    """
    artifact, path = _certified(tmp_path, overconfident=True)
    mapped = _mapped(artifact)
    assert mapped < 0.5 <= RAW, "fixture must separate the mapped value from the raw"

    signals = _signals(monkeypatch, path)

    assert signals.hypothesis_confidence_calibrated is True
    assert abs(signals.hypothesis_confidence - mapped) < 1e-9
    assert signals.hypothesis_confidence != RAW
    assessment = classify_severity(signals)
    assert assessment.rounded_up is True
    assert "calibrated diagnosis probability" in assessment.rationale


def test_the_raw_self_report_survives_as_evidence(tmp_path, monkeypatch):
    """Overruling the model's number is not a reason to hide it."""
    artifact, path = _certified(tmp_path, overconfident=True)

    signals = _signals(monkeypatch, path)

    raw_links = [
        link
        for link in signals.evidence
        if link.field == "hypothesis_confidence"
        and link.source == "reflector_analysis"
    ]
    assert len(raw_links) == 1
    assert raw_links[0].value == RAW

    mapped_links = [
        link
        for link in signals.evidence
        if link.field == "hypothesis_confidence_calibrated"
    ]
    assert len(mapped_links) == 1
    assert abs(mapped_links[0].value - _mapped(artifact)) < 1e-9
    expected_source = f"calibration_artifact:{artifact.artifact_version}"
    assert mapped_links[0].source == expected_source


def test_no_artifact_leaves_the_self_report_uncalibrated(monkeypatch):
    """With nothing configured the raw number is kept, and it decides nothing."""
    signals = _signals(monkeypatch, None)

    assert signals.hypothesis_confidence_calibrated is False
    assert signals.hypothesis_confidence == RAW
    assert classify_severity(signals).rounded_up is True
    assert not [
        link
        for link in signals.evidence
        if link.field == "hypothesis_confidence_calibrated"
    ]


def test_a_missing_config_fingerprint_is_not_calibration(tmp_path, monkeypatch):
    """A certified artifact built for another configuration must not apply."""
    _, path = _certified(tmp_path, overconfident=False)
    monkeypatch.setenv("DIAGNOSIS_CONFIDENCE_CALIBRATION_PATH", str(path))
    monkeypatch.delenv("SENTINEL_CONFIG_FINGERPRINT", raising=False)

    signals = extract_incident_signals(_state())

    assert signals.hypothesis_confidence_calibrated is False
    assert signals.hypothesis_confidence == RAW
    assert classify_severity(signals).rounded_up is True
