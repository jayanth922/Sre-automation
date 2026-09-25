#!/usr/bin/env python3
"""The trace artifact a trial cites has to be that trial's trace.

Every trial used to record the same constant path, which made the release
gate's artifact cross-check unfalsifiable and left the evidence in one shared
file that no campaign directory carried.
"""

import json

import pytest

from sre_agent import trace_evidence


@pytest.fixture
def base(tmp_path, monkeypatch):
    target = tmp_path / "run-trace.jsonl"
    monkeypatch.setenv("TRACE_EVIDENCE_PATH", str(target))
    return target


def _run(recorder, trace_id):
    recorder.start_run(
        root_trace_id=trace_id,
        run_manifest_id="manifest-1",
        incident_id="incident-1",
        job_id="job-1",
    )


def test_two_runs_do_not_share_one_artifact(base):
    recorder = trace_evidence.RunTraceRecorder()
    _run(recorder, "trace-aaa")
    _run(recorder, "trace-bbb")

    first = trace_evidence._artifact_path("trace-aaa")
    second = trace_evidence._artifact_path("trace-bbb")
    assert first != second
    assert first.exists() and second.exists()
    assert json.loads(first.read_text().splitlines()[0])["root_trace_id"] == "trace-aaa"
    assert json.loads(second.read_text().splitlines()[0])["root_trace_id"] == "trace-bbb"


def test_a_run_reports_its_own_artifact_not_the_shared_one(base):
    recorder = trace_evidence.RunTraceRecorder()
    _run(recorder, "trace-ccc")

    summary = recorder.summary(root_trace_id="trace-ccc", model_accounting={})

    assert summary["artifact_path"] != str(base)
    assert summary["artifact_path"] == str(trace_evidence._artifact_path("trace-ccc"))
    assert "trace-ccc" in summary["artifact_path"]


def test_the_artifact_path_still_honours_its_configured_base(base, tmp_path):
    assert trace_evidence._artifact_path("trace-ddd").is_relative_to(tmp_path)
    assert trace_evidence._artifact_path("trace-ddd").suffix == ".jsonl"


def test_a_record_without_a_trace_id_keeps_the_base_file(base):
    recorder = trace_evidence.RunTraceRecorder()
    recorder._append({"record_type": "orphan", "root_trace_id": None})

    assert base.exists()
    assert json.loads(base.read_text().splitlines()[0])["record_type"] == "orphan"


def test_a_trace_id_cannot_escape_its_directory(base, tmp_path):
    path = trace_evidence._artifact_path("../../etc/passwd")

    assert ".." not in path.parts
    assert path.is_relative_to(tmp_path)
