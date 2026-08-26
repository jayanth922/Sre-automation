#!/usr/bin/env python3
"""Tests for versioned benchmark scenario datasets."""

import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS = ROOT / "benchmarks"
DATASETS = BENCHMARKS / "datasets"
sys.path.insert(0, str(BENCHMARKS))

_MODULE_PATH = BENCHMARKS / "scenario_dataset.py"
_spec = importlib.util.spec_from_file_location("scenario_dataset", _MODULE_PATH)
dataset_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = dataset_module
_spec.loader.exec_module(dataset_module)


def test_v1_train_and_dev_splits_load_with_required_ground_truth():
    train = dataset_module.load_dataset(DATASETS, "v1", "train")
    dev = dataset_module.load_dataset(DATASETS, "v1", "dev")

    assert train.dataset_version == dev.dataset_version == "sentinel-sre-v1"
    assert train.split == "train"
    assert dev.split == "dev"
    assert train.scenarios and dev.scenarios

    for scenario in (*train.scenarios, *dev.scenarios):
        assert scenario.dataset_version == "sentinel-sre-v1"
        assert scenario.scenario_version
        assert scenario.risk_class in {"low", "medium", "high", "critical"}
        assert scenario.expected_evidence
        assert scenario.provenance["source"]
        assert scenario.taxonomy["category"]
        assert scenario.fault["adapter"] == "meridian_admin_config_v1"
        assert scenario.fault["target"]
        assert scenario.recovery_probe.query
        assert not (scenario.expected_action_types & scenario.unsafe_action_types)


def test_holdout_is_frozen_and_blocked_without_explicit_local_access():
    with pytest.raises(dataset_module.DatasetError, match="protected"):
        dataset_module.load_dataset(DATASETS, "v1", "holdout")

    holdout = dataset_module.load_dataset(
        DATASETS, "v1", "holdout", allow_holdout=True, ci=False
    )
    assert holdout.frozen is True
    assert holdout.scenarios

    with pytest.raises(dataset_module.DatasetError, match="CI"):
        dataset_module.load_dataset(
            DATASETS, "v1", "holdout", allow_holdout=True, ci=True
        )


def test_split_files_are_content_addressed_and_scenario_ids_do_not_overlap():
    index = json.loads((DATASETS / "v1" / "dataset.json").read_text())
    seen: set[str] = set()

    for split in ("train", "dev", "holdout"):
        metadata = index["splits"][split]
        path = DATASETS / "v1" / metadata["file"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == metadata["sha256"]
        loaded = dataset_module.load_dataset(
            DATASETS,
            "v1",
            split,
            allow_holdout=split == "holdout",
            ci=False,
        )
        current = {scenario.name for scenario in loaded.scenarios}
        assert not (seen & current)
        seen.update(current)


def test_tampered_split_fails_digest_validation(tmp_path):
    copied = tmp_path / "datasets"
    shutil.copytree(DATASETS, copied)
    train_path = copied / "v1" / "train.json"
    train_path.write_text(f"{train_path.read_text()}\n")

    with pytest.raises(dataset_module.DatasetError, match="digest"):
        dataset_module.load_dataset(copied, "v1", "train")


def test_manifest_rejects_allowed_and_forbidden_action_overlap(tmp_path):
    copied = tmp_path / "datasets"
    shutil.copytree(DATASETS, copied)
    version_root = copied / "v1"
    train_path = version_root / "train.json"
    payload = json.loads(train_path.read_text())
    action = payload["scenarios"][0]["allowed_action_types"][0]
    payload["scenarios"][0]["forbidden_action_types"].append(action)
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    train_path.write_text(encoded)

    index_path = version_root / "dataset.json"
    index = json.loads(index_path.read_text())
    index["splits"]["train"]["sha256"] = hashlib.sha256(encoded.encode()).hexdigest()
    index_path.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")

    with pytest.raises(dataset_module.DatasetError, match="overlap"):
        dataset_module.load_dataset(copied, "v1", "train")


def test_live_runner_loads_selected_split_instead_of_inline_scenarios():
    source = (BENCHMARKS / "sre_bench.py").read_text()

    assert "load_dataset(" in source
    assert "BENCH_DATASET_SPLIT" in source
    assert "BENCH_FAULT_MODE" in source
    assert "MeridianAdminConfigAdapter" in source
    assert "dataset_sha256=DATASET.sha256" in source
    assert "SCENARIOS = [" not in source
