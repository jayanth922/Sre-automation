import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "benchmarks" / "datasets"
sys.path.append(str(ROOT / "benchmarks"))

import statistical_smoke_dataset  # noqa: E402
from scenario_dataset import load_dataset  # noqa: E402


def _source_copy(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    shutil.copytree(SOURCE_ROOT / "v2", root / "v2")
    return root


def test_builds_one_scenario_with_a_distinct_content_identity(tmp_path):
    source_root = _source_copy(tmp_path)
    source = load_dataset(source_root, "v2", "dev")
    scenario = source.scenarios[0].name
    output_root = tmp_path / "derived"

    result = statistical_smoke_dataset.build_smoke_dataset(
        source_root=source_root,
        source_version="v2",
        split="dev",
        scenario=scenario,
        output_root=output_root,
        output_version="smoke",
    )

    derived = load_dataset(output_root, "smoke", "dev")
    assert [item.name for item in derived.scenarios] == [scenario]
    assert derived.sha256 != source.sha256
    assert result["source"]["sha256"] == source.sha256
    assert result["derived"]["sha256"] == derived.sha256
    assert result["selection"]["runs_per_scenario"] == 1
    provenance = json.loads(
        (output_root / "smoke" / "smoke-provenance.json").read_text()
    )
    assert provenance["derived"]["scenario_count"] == 1


def test_other_splits_remain_byte_identical(tmp_path):
    source_root = _source_copy(tmp_path)
    source = load_dataset(source_root, "v2", "dev")
    output_root = tmp_path / "derived"

    statistical_smoke_dataset.build_smoke_dataset(
        source_root=source_root,
        source_version="v2",
        split="dev",
        scenario=source.scenarios[0].name,
        output_root=output_root,
        output_version="smoke",
    )

    for filename in ("train.json", "holdout.json", "fixtures.json"):
        assert (source_root / "v2" / filename).read_bytes() == (
            output_root / "smoke" / filename
        ).read_bytes()


def test_unknown_scenario_fails_before_creating_output(tmp_path):
    source_root = _source_copy(tmp_path)
    output_root = tmp_path / "derived"

    with pytest.raises(statistical_smoke_dataset.SmokeDatasetError, match="available"):
        statistical_smoke_dataset.build_smoke_dataset(
            source_root=source_root,
            source_version="v2",
            split="dev",
            scenario="does-not-exist",
            output_root=output_root,
            output_version="smoke",
        )

    assert not output_root.exists()


def test_existing_output_is_never_overwritten(tmp_path):
    source_root = _source_copy(tmp_path)
    source = load_dataset(source_root, "v2", "dev")
    output_root = tmp_path / "derived"
    output_version_root = output_root / "smoke"
    output_version_root.mkdir(parents=True)
    sentinel = output_version_root / "keep"
    sentinel.write_text("existing")

    with pytest.raises(statistical_smoke_dataset.SmokeDatasetError, match="exists"):
        statistical_smoke_dataset.build_smoke_dataset(
            source_root=source_root,
            source_version="v2",
            split="dev",
            scenario=source.scenarios[0].name,
            output_root=output_root,
            output_version="smoke",
        )

    assert sentinel.read_text() == "existing"


def test_holdout_cannot_be_used_for_a_smoke(tmp_path):
    with pytest.raises(
        statistical_smoke_dataset.SmokeDatasetError, match="train or dev"
    ):
        statistical_smoke_dataset.build_smoke_dataset(
            source_root=SOURCE_ROOT,
            source_version="v2",
            split="holdout",
            scenario="anything",
            output_root=tmp_path,
            output_version="smoke",
        )
