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

VERSIONS = ("v1", "v2")


def _repin(version_root: Path, name: str, payload: dict) -> None:
    """Write a split or manifest back and re-pin its digest in the index."""
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    (version_root / f"{name}.json").write_text(encoded)
    index_path = version_root / "dataset.json"
    index = json.loads(index_path.read_text())
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    if name == "fixtures":
        index["fixtures"]["sha256"] = digest
    else:
        index["splits"][name]["sha256"] = digest
    index_path.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")


@pytest.fixture
def v2_root(tmp_path):
    """A writable copy of v2 whose splits can be mutated and re-pinned."""
    copied = tmp_path / "datasets"
    shutil.copytree(DATASETS, copied)
    return copied


def _mutate_train(v2_root: Path, mutate) -> None:
    version_root = v2_root / "v2"
    payload = json.loads((version_root / "train.json").read_text())
    mutate(payload["scenarios"][0])
    _repin(version_root, "train", payload)


@pytest.mark.parametrize("version", VERSIONS)
def test_train_and_dev_splits_load_with_required_ground_truth(version):
    train = dataset_module.load_dataset(DATASETS, version, "train")
    dev = dataset_module.load_dataset(DATASETS, version, "dev")
    expected_version = f"sentinel-sre-{version}"

    assert train.dataset_version == dev.dataset_version == expected_version
    assert train.split == "train"
    assert dev.split == "dev"
    assert train.scenarios and dev.scenarios

    for scenario in (*train.scenarios, *dev.scenarios):
        assert scenario.dataset_version == expected_version
        assert scenario.scenario_version
        assert scenario.risk_class in {"low", "medium", "high", "critical"}
        assert scenario.expected_evidence
        assert scenario.provenance["source"]
        assert scenario.taxonomy["category"]
        assert scenario.fault["adapter"] == "meridian_admin_config_v1"
        assert scenario.fault["contracts"]
        for contract in scenario.fault["contracts"]:
            assert contract["target"]
            assert contract["path"].startswith("/")
            assert set(contract["inject"]) == set(contract["cleanup"])
        assert scenario.recovery_probe.query
        assert not (scenario.expected_action_types & scenario.unsafe_action_types)


def test_v2_is_broad_enough_for_the_downstream_measurements():
    """#20's whole point: enough varied scenarios to make #21/#22 meaningful."""
    splits = {
        split: dataset_module.load_dataset(
            DATASETS, "v2", split, allow_holdout=split == "holdout", ci=False
        )
        for split in ("train", "dev", "holdout")
    }
    scenarios = [s for split in splits.values() for s in split.scenarios]
    assert len(scenarios) >= 20
    assert all(split.scenarios for split in splits.values())

    categories = {s.taxonomy["category"] for s in scenarios}
    assert len(categories) >= 6
    assert {"multi_fault", "noisy", "clean", "dependency"} <= categories

    # Distinct failure surfaces, not one fault relabelled.
    fault_modes = {s.taxonomy["fault_mode"] for s in scenarios}
    assert len(fault_modes) >= 8
    assert len({s.ground_truth_service for s in scenarios}) >= 4

    # Cross-service scenarios exist and are the reason schema 2 exists.
    multi = [s for s in scenarios if len(s.fault["contracts"]) > 1]
    assert len(multi) >= 3

    # A correct answer is sometimes "do nothing"; those must not arm the
    # recovery oracle, which would report INVALID_SCENARIO for a healthy probe.
    no_action = [s for s in scenarios if not s.expected_action_types]
    assert len(no_action) >= 3
    for scenario in no_action:
        assert scenario.recovery_probe.require_failure_observation is False
    for scenario in scenarios:
        if scenario.expected_action_types - {"escalate"}:
            assert scenario.recovery_probe.require_failure_observation is True


def test_holdout_is_frozen_and_blocked_without_explicit_local_access():
    # Force non-CI for the protected-access path; GitHub Actions sets CI=true
    # which would otherwise short-circuit to the CI-unavailable error first.
    with pytest.raises(dataset_module.DatasetError, match="protected"):
        dataset_module.load_dataset(DATASETS, "v2", "holdout", ci=False)

    holdout = dataset_module.load_dataset(
        DATASETS, "v2", "holdout", allow_holdout=True, ci=False
    )
    assert holdout.frozen is True
    assert holdout.scenarios

    with pytest.raises(dataset_module.DatasetError, match="CI"):
        dataset_module.load_dataset(
            DATASETS, "v2", "holdout", allow_holdout=True, ci=True
        )


@pytest.mark.parametrize("version", VERSIONS)
def test_split_files_are_content_addressed_and_ids_do_not_overlap(version):
    index = json.loads((DATASETS / version / "dataset.json").read_text())
    seen: set[str] = set()

    for split in ("train", "dev", "holdout"):
        metadata = index["splits"][split]
        path = DATASETS / version / metadata["file"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == metadata["sha256"]
        loaded = dataset_module.load_dataset(
            DATASETS,
            version,
            split,
            allow_holdout=split == "holdout",
            ci=False,
        )
        current = {scenario.name for scenario in loaded.scenarios}
        assert not (seen & current)
        seen.update(current)


@pytest.mark.parametrize("version", VERSIONS)
def test_tampered_split_fails_digest_validation(tmp_path, version):
    copied = tmp_path / "datasets"
    shutil.copytree(DATASETS, copied)
    train_path = copied / version / "train.json"
    train_path.write_text(f"{train_path.read_text()}\n")

    with pytest.raises(dataset_module.DatasetError, match="digest"):
        dataset_module.load_dataset(copied, version, "train")


def test_tampered_fixture_manifest_fails_digest_validation(v2_root):
    manifest = v2_root / "v2" / "fixtures.json"
    manifest.write_text(f"{manifest.read_text()}\n")

    with pytest.raises(dataset_module.DatasetError, match="digest"):
        dataset_module.load_dataset(v2_root, "v2", "train")


def test_manifest_rejects_allowed_and_forbidden_action_overlap(v2_root):
    def mutate(scenario):
        scenario["forbidden_action_types"].append(scenario["allowed_action_types"][0])

    _mutate_train(v2_root, mutate)
    with pytest.raises(dataset_module.DatasetError, match="overlap"):
        dataset_module.load_dataset(v2_root, "v2", "train")


def test_fixture_manifest_rejects_a_target_the_workload_does_not_expose(v2_root):
    def mutate(scenario):
        scenario["fault"]["contracts"][0]["target"] = "recommendation-service"

    _mutate_train(v2_root, mutate)
    with pytest.raises(dataset_module.DatasetError, match="undeclared fixture target"):
        dataset_module.load_dataset(v2_root, "v2", "train")


def test_fixture_manifest_rejects_a_knob_the_service_does_not_have(v2_root):
    def mutate(scenario):
        contract = scenario["fault"]["contracts"][0]
        contract["inject"]["payload"] = {"cpu_throttle": 0.9}
        contract["cleanup"]["payload"] = {"cpu_throttle": 0.0}

    _mutate_train(v2_root, mutate)
    with pytest.raises(dataset_module.DatasetError, match="undeclared knob"):
        dataset_module.load_dataset(v2_root, "v2", "train")


def test_fixture_manifest_rejects_a_value_outside_the_declared_range(v2_root):
    def mutate(scenario):
        scenario["fault"]["contracts"][0]["inject"]["payload"] = {"error_rate": 1.5}

    _mutate_train(v2_root, mutate)
    with pytest.raises(dataset_module.DatasetError, match="outside the declared range"):
        dataset_module.load_dataset(v2_root, "v2", "train")


def test_fixture_manifest_rejects_cleanup_that_is_not_the_real_baseline(v2_root):
    def mutate(scenario):
        scenario["fault"]["contracts"][0]["cleanup"]["payload"] = {"error_rate": 0.01}

    _mutate_train(v2_root, mutate)
    with pytest.raises(dataset_module.DatasetError, match="fixture baseline"):
        dataset_module.load_dataset(v2_root, "v2", "train")


def test_fixture_manifest_rejects_a_fault_path_the_target_does_not_serve(v2_root):
    def mutate(scenario):
        contract = scenario["fault"]["contracts"][0]
        contract["inject"]["path"] = "/internal/config"
        contract["cleanup"]["path"] = "/internal/config"

    _mutate_train(v2_root, mutate)
    with pytest.raises(dataset_module.DatasetError, match="declared config path"):
        dataset_module.load_dataset(v2_root, "v2", "train")


def test_fixture_manifest_rejects_an_invented_alert_rule(v2_root):
    def mutate(scenario):
        scenario["alert"]["alertname"] = "CheckoutQueueBacklog"

    _mutate_train(v2_root, mutate)
    with pytest.raises(dataset_module.DatasetError, match="undeclared alert"):
        dataset_module.load_dataset(v2_root, "v2", "train")


def test_fixture_manifest_rejects_an_alert_severity_the_rule_never_emits(v2_root):
    def mutate(scenario):
        scenario["alert"]["severity"] = "warning"

    _mutate_train(v2_root, mutate)
    with pytest.raises(dataset_module.DatasetError, match="contradicts the declared"):
        dataset_module.load_dataset(v2_root, "v2", "train")


def test_fixture_manifest_rejects_a_probe_on_a_metric_nothing_exports(v2_root):
    def mutate(scenario):
        scenario["recovery_probe"]["query"] = "sum(rate(checkout_orders_lost[5m]))"

    _mutate_train(v2_root, mutate)
    with pytest.raises(dataset_module.DatasetError, match="no declared metric"):
        dataset_module.load_dataset(v2_root, "v2", "train")


def test_schema_2_rejects_a_scenario_that_degrades_one_target_twice(v2_root):
    def mutate(scenario):
        contract = scenario["fault"]["contracts"][0]
        scenario["fault"]["contracts"].append(
            {
                "target": contract["target"],
                "inject": {"path": "/admin/config", "payload": {"slow_rate": 0.5}},
                "cleanup": {"path": "/admin/config", "payload": {"slow_rate": 0.0}},
            }
        )

    _mutate_train(v2_root, mutate)
    with pytest.raises(dataset_module.DatasetError, match="twice"):
        dataset_module.load_dataset(v2_root, "v2", "train")


def test_repin_restores_content_addressing_after_a_deliberate_edit(v2_root):
    version_root = v2_root / "v2"
    payload = json.loads((version_root / "dev.json").read_text())
    payload["scenarios"][0]["expected_evidence"].append("a newly required fact")
    (version_root / "dev.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n"
    )
    with pytest.raises(dataset_module.DatasetError, match="digest"):
        dataset_module.load_dataset(v2_root, "v2", "dev")

    digests = dataset_module.repin(v2_root, "v2")
    assert set(digests) == {"train.json", "dev.json", "holdout.json", "fixtures.json"}
    reloaded = dataset_module.load_dataset(v2_root, "v2", "dev")
    assert "a newly required fact" in reloaded.scenarios[0].expected_evidence


def test_repin_refuses_to_bless_a_dataset_that_does_not_load(v2_root):
    def mutate(scenario):
        scenario["alert"]["alertname"] = "CheckoutQueueBacklog"

    _mutate_train(v2_root, mutate)
    with pytest.raises(dataset_module.DatasetError, match="undeclared alert"):
        dataset_module.repin(v2_root, "v2")


def test_live_runner_loads_selected_split_instead_of_inline_scenarios():
    source = (BENCHMARKS / "sre_bench.py").read_text()

    assert "load_dataset(" in source
    assert "BENCH_DATASET_SPLIT" in source
    assert "BENCH_FAULT_MODE" in source
    assert "MeridianAdminConfigAdapter" in source
    assert "dataset_sha256=DATASET.sha256" in source
    assert "SCENARIOS = DATASET.scenarios" in source
    # The guard is against scenarios defined in the runner, which is the drift
    # this test was added to catch. It used to be spelled `"SCENARIOS = [" not
    # in source`, which also forbade *narrowing* the dataset's own list — so
    # the smoke filter tripped it. Constructing a spec is the thing that must
    # never happen here; the runner only ever annotates with the type.
    assert "ScenarioSpec(" not in source
    # Every v2 fault target must be reachable or automatic mode cannot inject.
    manifest = json.loads((DATASETS / "v2" / "fixtures.json").read_text())
    for target in manifest["targets"]:
        assert f'"{target}"' in source


# --- The smoke-run scenario filter -------------------------------------------
#
# `BENCH_SCENARIOS` narrows a run to a named subset so "does the ACT path work
# at all" does not cost a whole split. Both of its refusals are load-bearing:
# a typo that fell back to the full split would turn a slip into a bill, and a
# subset accepted during a recorded experiment would emit trials stamped with
# the full split's `dataset_sha256` in an order `BENCH_PAIR_SEED` no longer
# controls.


def _load_runner(monkeypatch, **env):
    """Import `sre_bench` fresh under a controlled environment.

    The filter runs at import time, next to the dataset load it narrows, so
    there is no function to call — the module either builds a narrowed
    `SCENARIOS` or raises.
    """
    for key in ("BENCH_SCENARIOS", "BENCH_EXPERIMENT_ID", "BENCH_CANDIDATE_ID",
                "BENCH_TRIAL_RESULTS_PATH", "BENCH_CONFIDENCE_RESULTS_PATH",
                "BENCH_CONFIG_FINGERPRINT", "BENCH_PAIR_SEED"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    spec = importlib.util.spec_from_file_location(
        "sre_bench_under_test", BENCHMARKS / "sre_bench.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_no_filter_runs_the_whole_split(monkeypatch):
    module = _load_runner(monkeypatch)

    assert module.SCENARIO_FILTER == ()
    assert len(module.SCENARIOS) == len(module.DATASET.scenarios)


def test_a_named_scenario_narrows_the_run_to_itself(monkeypatch):
    full = _load_runner(monkeypatch)
    chosen = full.DATASET.scenarios[0].name

    module = _load_runner(monkeypatch, BENCH_SCENARIOS=chosen)

    assert [spec.name for spec in module.SCENARIOS] == [chosen]


def test_whitespace_and_multiple_names_are_accepted(monkeypatch):
    full = _load_runner(monkeypatch)
    first, second = (spec.name for spec in full.DATASET.scenarios[:2])

    module = _load_runner(monkeypatch, BENCH_SCENARIOS=f" {first} , {second} ")

    assert {spec.name for spec in module.SCENARIOS} == {first, second}


def test_an_unknown_name_raises_rather_than_running_everything(monkeypatch):
    """Falling back to the full split would make a typo cost a whole run."""
    with pytest.raises(RuntimeError, match="not in"):
        _load_runner(monkeypatch, BENCH_SCENARIOS="no_such_scenario")


def test_the_error_lists_what_was_available(monkeypatch):
    with pytest.raises(RuntimeError, match="Available:"):
        _load_runner(monkeypatch, BENCH_SCENARIOS="no_such_scenario")


# Statistical recording is all-or-nothing: a partially-set experiment raises
# its own error, whose text also contains "statistical recording". Matching
# loosely here would let these two tests pass without the subset guard
# existing at all, so they set every field and match on the subset wording.
_EXPERIMENT_ENV = {
    "BENCH_EXPERIMENT_ID": "exp-1",
    "BENCH_CANDIDATE_ID": "full",
    # A real digest: the runner now rejects a fingerprint that is not one.
    "BENCH_CONFIG_FINGERPRINT": "a" * 64,
    "BENCH_PAIR_SEED": "seed-1",
}


def test_a_subset_is_refused_during_a_recorded_experiment(monkeypatch):
    """The trials would carry the full split's sha in an unseeded order."""
    full = _load_runner(monkeypatch)
    chosen = full.DATASET.scenarios[0].name

    with pytest.raises(RuntimeError, match="smoke-run filter"):
        _load_runner(monkeypatch, BENCH_SCENARIOS=chosen, **_EXPERIMENT_ENV)


def test_a_recorded_experiment_without_a_subset_is_untouched(monkeypatch):
    module = _load_runner(monkeypatch, **_EXPERIMENT_ENV)

    assert module.STATISTICAL_RECORDING is True
    assert len(module.SCENARIOS) == len(module.DATASET.scenarios)
