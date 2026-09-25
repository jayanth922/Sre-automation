#!/usr/bin/env python3
"""Build a one-scenario, content-addressed dataset for a statistical smoke."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Optional, Sequence

from scenario_dataset import DatasetError, load_dataset

SCHEMA_VERSION = 1


class SmokeDatasetError(ValueError):
    """A requested smoke dataset would not be bounded or attributable."""


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def build_smoke_dataset(
    *,
    source_root: Path,
    source_version: str,
    split: str,
    scenario: str,
    output_root: Path,
    output_version: str,
) -> dict[str, Any]:
    """Copy a pinned dataset and narrow one non-holdout split to one scenario."""
    if split not in {"train", "dev"}:
        raise SmokeDatasetError("statistical smoke split must be train or dev")
    if not scenario.strip():
        raise SmokeDatasetError("scenario must be a non-empty name")
    if not output_version.strip() or Path(output_version).name != output_version:
        raise SmokeDatasetError("output version must be one directory name")

    try:
        source = load_dataset(source_root, source_version, split, ci=False)
    except DatasetError as exc:
        raise SmokeDatasetError(f"source dataset is invalid: {exc}") from exc

    matches = [item for item in source.scenarios if item.name == scenario]
    if len(matches) != 1:
        available = sorted(item.name for item in source.scenarios)
        raise SmokeDatasetError(
            f"scenario {scenario!r} is not unique in {source_version}/{split}; "
            f"available={available}"
        )

    source_version_root = source_root / source_version
    output_version_root = output_root / output_version
    if output_version_root.exists():
        raise SmokeDatasetError(f"output already exists: {output_version_root}")
    output_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_version_root, output_version_root)

    index_path = output_version_root / "dataset.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    split_filename = index["splits"][split]["file"]
    split_path = output_version_root / split_filename
    split_payload = json.loads(split_path.read_text(encoding="utf-8"))
    selected = [
        item for item in split_payload["scenarios"] if item.get("id") == scenario
    ]
    if len(selected) != 1:
        raise SmokeDatasetError("validated scenario was not found in source JSON")
    split_payload["scenarios"] = selected
    split_raw = _json_bytes(split_payload)
    split_path.write_bytes(split_raw)
    derived_sha = _sha256(split_raw)
    index["description"] = (
        f"One-scenario statistical smoke derived from {source_version}/{split} "
        f"at {source.sha256}; not a full-split measurement."
    )
    index["splits"][split]["sha256"] = derived_sha
    index_path.write_bytes(_json_bytes(index))

    try:
        derived = load_dataset(output_root, output_version, split, ci=False)
    except DatasetError as exc:
        raise SmokeDatasetError(f"derived dataset is invalid: {exc}") from exc
    if len(derived.scenarios) != 1 or derived.scenarios[0].name != scenario:
        raise SmokeDatasetError("derived dataset did not remain exactly one scenario")

    provenance = {
        "schema_version": SCHEMA_VERSION,
        "kind": "statistical_smoke_dataset",
        "source": {
            "version_directory": source_version,
            "dataset_version": source.dataset_version,
            "split": split,
            "sha256": source.sha256,
        },
        "selection": {"scenario": scenario, "runs_per_scenario": 1},
        "derived": {
            "version_directory": output_version,
            "dataset_version": derived.dataset_version,
            "split": derived.split,
            "sha256": derived.sha256,
            "scenario_count": len(derived.scenarios),
        },
    }
    provenance_raw = _json_bytes(provenance)
    (output_version_root / "smoke-provenance.json").write_bytes(provenance_raw)
    return {**provenance, "provenance_sha256": _sha256(provenance_raw)}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--source-root", type=Path, default=Path(__file__).parent / "datasets"
    )
    parser.add_argument("--source-version", default="v2")
    parser.add_argument("--split", choices=("train", "dev"), default="dev")
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--output-version", default="statistical-smoke")
    args = parser.parse_args(argv)
    try:
        result = build_smoke_dataset(
            source_root=args.source_root,
            source_version=args.source_version,
            split=args.split,
            scenario=args.scenario,
            output_root=args.output_root,
            output_version=args.output_version,
        )
    except (OSError, SmokeDatasetError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
