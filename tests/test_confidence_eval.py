#!/usr/bin/env python3
"""CLI coverage for A06 confidence reports and artifacts."""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks"))
sys.path.insert(1, str(ROOT))

import confidence_eval  # noqa: E402

from sre_agent.confidence_calibration import (  # noqa: E402
    append_confidence_record,
    build_confidence_record,
    load_calibration_artifact,
)

CONFIG_FINGERPRINT = "c" * 64


def test_cli_writes_report_and_config_pinned_artifact(tmp_path, monkeypatch):
    records_path = tmp_path / "confidence.jsonl"
    report_path = tmp_path / "report.json"
    artifact_path = tmp_path / "artifact.json"
    observed_at = datetime(2026, 8, 26, tzinfo=timezone.utc)
    for index in range(200):
        high = index >= 100
        append_confidence_record(
            records_path,
            build_confidence_record(
                task="remediation",
                rubric_version="sre-structured-v1",
                raw_confidence=0.9 if high else 0.2,
                outcome=high,
                scenario="checkout_bad_deploy",
                scenario_version="1.0.0",
                dataset_sha256="d" * 64,
                config_fingerprint=CONFIG_FINGERPRINT,
                pair_id=f"pair-{index}",
                observed_at=observed_at + timedelta(seconds=index),
            ),
        )

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "confidence_eval.py",
            str(records_path),
            "--task",
            "remediation",
            "--config-fingerprint",
            CONFIG_FINGERPRINT,
            "--report-output",
            str(report_path),
            "--artifact-output",
            str(artifact_path),
            "--artifact-version",
            "remediation-v1",
        ],
    )

    assert confidence_eval.main() == 0
    report = json.loads(report_path.read_text())
    artifact = load_calibration_artifact(artifact_path)
    assert report["reliability"]["samples"] == 200
    assert artifact.config_fingerprint == CONFIG_FINGERPRINT
    assert artifact.autonomy_threshold is not None
