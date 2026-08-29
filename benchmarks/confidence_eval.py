#!/usr/bin/env python3
"""Build confidence reliability reports, calibration artifacts, and drift checks."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sre_agent.confidence_calibration import (  # noqa: E402
    ConfidenceCalibrationError,
    build_calibration_artifact,
    calibration_drift,
    load_confidence_records,
    reliability_report,
    save_calibration_artifact,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate task-specific SRE confidence calibration"
    )
    parser.add_argument("records", type=Path, help="Confidence JSONL evidence")
    parser.add_argument(
        "--task",
        choices=("diagnosis", "remediation"),
        required=True,
    )
    parser.add_argument(
        "--config-fingerprint",
        required=True,
        help="Lowercase SHA-256 of the evaluated A01 configuration",
    )
    parser.add_argument("--report-output", type=Path, required=True)
    parser.add_argument("--reference-records", type=Path)
    parser.add_argument("--artifact-output", type=Path)
    parser.add_argument("--artifact-version")
    parser.add_argument("--minimum-samples", type=int, default=100)
    parser.add_argument("--minimum-bin-samples", type=int, default=20)
    parser.add_argument("--maximum-bins", type=int, default=10)
    parser.add_argument("--minimum-threshold-support", type=int, default=40)
    parser.add_argument("--required-wilson-lower", type=float, default=0.90)
    args = parser.parse_args()

    try:
        records, source_sha = load_confidence_records(args.records)
        report = reliability_report(
            records,
            task=args.task,
            config_fingerprint=args.config_fingerprint,
        )
        output = {
            "schema_version": 1,
            "task": args.task,
            "source": {
                "path": str(args.records),
                "sha256": source_sha,
                "records": len(records),
            },
            "reliability": report.to_dict(),
            "drift": None,
            "calibration_artifact": None,
        }
        if args.reference_records:
            reference_records, reference_sha = load_confidence_records(
                args.reference_records
            )
            reference = reliability_report(
                reference_records,
                task=args.task,
                config_fingerprint=args.config_fingerprint,
            )
            output["reference"] = {
                "path": str(args.reference_records),
                "sha256": reference_sha,
                "records": len(reference_records),
            }
            output["drift"] = calibration_drift(reference, report)
        if args.artifact_output:
            if not args.artifact_version:
                raise ConfidenceCalibrationError(
                    "--artifact-version is required with --artifact-output"
                )
            artifact = build_calibration_artifact(
                records,
                task=args.task,
                source_sha256=source_sha,
                config_fingerprint=args.config_fingerprint,
                artifact_version=args.artifact_version,
                minimum_samples=args.minimum_samples,
                minimum_bin_samples=args.minimum_bin_samples,
                maximum_bins=args.maximum_bins,
                minimum_threshold_support=args.minimum_threshold_support,
                required_wilson_lower=args.required_wilson_lower,
            )
            save_calibration_artifact(args.artifact_output, artifact)
            output["calibration_artifact"] = {
                "path": str(args.artifact_output),
                "sha256": artifact.artifact_sha256,
                "autonomy_threshold": artifact.autonomy_threshold,
                "threshold_support": artifact.threshold_support,
                "threshold_wilson_lower": artifact.threshold_wilson_lower,
            }
    except ConfidenceCalibrationError as exc:
        parser.error(str(exc))

    args.report_output.parent.mkdir(parents=True, exist_ok=True)
    args.report_output.write_text(
        json.dumps(output, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    drifted = (
        isinstance(output.get("drift"), dict)
        and output["drift"].get("status") == "DRIFTED"
    )
    print(
        json.dumps(
            {
                "task": args.task,
                "samples": report.samples,
                "drift": (
                    output.get("drift", {}).get("status")
                    if output.get("drift")
                    else None
                ),
            },
            sort_keys=True,
        )
    )
    return 2 if drifted else 0


if __name__ == "__main__":
    raise SystemExit(main())
