"""The approval step the benchmark harness was missing.

`awaiting_approval` is terminal in TERMINAL_APPLICATION_STATUSES and nothing
clears it, so a plan the gate correctly holds for a human ended the trial there
and scored UNRESOLVED. These cover the two calls that close that gap -- the
same pair the dashboard makes -- and the bookkeeping that keeps an authorized
run distinguishable from an autonomous one.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS = ROOT / "benchmarks"
if str(BENCHMARKS) not in sys.path:
    sys.path.insert(0, str(BENCHMARKS))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scoring import RunScore  # noqa: E402

_BENCH_ENV = (
    "BENCH_SCENARIOS",
    "BENCH_EXPERIMENT_ID",
    "BENCH_CANDIDATE_ID",
    "BENCH_CONFIG_FINGERPRINT",
    "BENCH_PAIR_SEED",
    "BENCH_TRIAL_RESULTS_PATH",
    "BENCH_CONFIDENCE_RESULTS_PATH",
    "BENCH_GRADER_RESULTS_PATH",
    "BENCH_AUTO_APPROVE",
    "BENCH_AUTO_APPROVE_LIMIT",
)

HASH_A = "a" * 64
HASH_B = "b" * 64
REQUEST_A = "11111111-1111-1111-1111-111111111111"


def _load_runner(monkeypatch, tmp_path: Path, **env):
    """Import `sre_bench` fresh: it reads its config at import."""
    for key in _BENCH_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BENCH_GRADER_RESULTS_PATH", str(tmp_path / "grades.jsonl"))
    monkeypatch.setenv("BENCH_TRIAL_RESULTS_PATH", str(tmp_path / "trials.jsonl"))
    monkeypatch.setenv(
        "BENCH_CONFIDENCE_RESULTS_PATH", str(tmp_path / "confidence.jsonl")
    )
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    spec = importlib.util.spec_from_file_location(
        "sre_bench_approval_under_test", BENCHMARKS / "sre_bench.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, payload=None, status_code=200, text=""):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeClient:
    """Answers GET /status with a pending interrupt and records the POST."""

    def __init__(self, approval, post_response=None, status_response=None):
        self._approval = approval
        self._post_response = post_response or FakeResponse()
        self._status_response = status_response
        self.posts = []
        self.gets = []

    async def get(self, url, headers=None, **kwargs):
        self.gets.append(url)
        if self._status_response is not None:
            return self._status_response
        return FakeResponse({"status": "WAITING_APPROVAL", "approval": self._approval})

    async def post(self, url, headers=None, json=None, **kwargs):
        self.posts.append((url, json))
        return self._post_response


class FakeToken:
    async def headers(self):
        return {"Authorization": "Bearer test"}


class FakeCreds:
    base_url = "https://platform.test"


def _approval(request_id=REQUEST_A, action_hash=HASH_A):
    return {
        "approval_request_id": request_id,
        "action_hash": action_hash,
        "expires_at": "2026-09-23T00:00:00+00:00",
    }


def _grant(module, client, granted=None):
    import asyncio

    return asyncio.run(
        module._grant_pending_approval(
            client, FakeToken(), "incident-1", FakeCreds(), granted
            if granted is not None
            else set()
        )
    )


def test_the_pending_interrupt_is_approved_by_its_own_hash(monkeypatch, tmp_path):
    """The hash is the graph's guarantee that what was authorized is what runs,
    so it is echoed from the live interrupt, never reconstructed here."""
    module = _load_runner(monkeypatch, tmp_path)
    client = FakeClient(_approval())

    assert _grant(module, client) is True

    url, payload = client.posts[0]
    assert url == "https://platform.test/api/v1/incidents/incident-1/approve"
    assert payload == {"approval_request_id": REQUEST_A, "action_hash": HASH_A}
    assert client.gets == ["https://platform.test/api/v1/incidents/incident-1/status"]


def test_nothing_is_approved_when_nothing_is_pending(monkeypatch, tmp_path):
    module = _load_runner(monkeypatch, tmp_path)
    client = FakeClient(None)

    assert _grant(module, client) is False
    assert client.posts == []


def test_an_incomplete_interrupt_is_not_approved(monkeypatch, tmp_path):
    """A payload missing either field cannot be authorized, and guessing one
    is exactly what the action_hash exists to prevent."""
    module = _load_runner(monkeypatch, tmp_path)
    client = FakeClient({"approval_request_id": REQUEST_A})

    assert _grant(module, client) is False
    assert client.posts == []


def test_the_same_action_is_never_approved_twice(monkeypatch, tmp_path):
    """The graph republishes the same interrupt until it is decided; approving
    it again would 409 and would double-count in the grade."""
    module = _load_runner(monkeypatch, tmp_path)
    client = FakeClient(_approval())
    granted = set()

    assert _grant(module, client, granted) is True
    assert _grant(module, client, granted) is False
    assert len(client.posts) == 1

    # A genuinely different action still goes through.
    client_b = FakeClient(_approval(action_hash=HASH_B))
    assert _grant(module, client_b, granted) is True


def test_a_refused_approval_is_reported_not_raised(monkeypatch, tmp_path):
    """A 409 means the run moved on. It must not take the trial down with it."""
    module = _load_runner(monkeypatch, tmp_path)
    client = FakeClient(
        _approval(),
        post_response=FakeResponse(status_code=409, text="no longer pending"),
    )
    granted = set()

    assert _grant(module, client, granted) is False
    # Not banked, so a later poll may legitimately retry.
    assert granted == set()


def test_a_broken_status_read_does_not_raise(monkeypatch, tmp_path):
    module = _load_runner(monkeypatch, tmp_path)
    client = FakeClient(_approval(), status_response=FakeResponse(status_code=503))

    assert _grant(module, client) is False
    assert client.posts == []


def test_auto_approve_is_off_unless_asked_for(monkeypatch, tmp_path):
    """It changes what the trial measures, so it cannot be the default."""
    assert _load_runner(monkeypatch, tmp_path).AUTO_APPROVE is False
    assert _load_runner(monkeypatch, tmp_path, BENCH_AUTO_APPROVE="1").AUTO_APPROVE


def test_the_grade_records_who_authorized_the_run(monkeypatch, tmp_path):
    """Otherwise an authorized run reads back exactly like an autonomous one."""
    module = _load_runner(monkeypatch, tmp_path)
    spec = module.SCENARIOS[0]
    result = _FakeResult(spec.name)

    module._record_grade(spec, result, "fixed it", [], _score(spec.name), 2)

    row = json.loads((tmp_path / "grades.jsonl").read_text().splitlines()[0])
    assert row["harness_approvals"] == 2


def test_an_unaided_run_records_no_approvals(monkeypatch, tmp_path):
    module = _load_runner(monkeypatch, tmp_path)
    spec = module.SCENARIOS[0]

    module._record_grade(spec, _FakeResult(spec.name), "fixed it", [], _score(spec.name))

    row = json.loads((tmp_path / "grades.jsonl").read_text().splitlines()[0])
    assert row["harness_approvals"] == 0


def test_an_approved_trial_is_marked_in_the_statistical_record(monkeypatch, tmp_path):
    """An arm the harness authorized must never be diffed against one that ran
    unaided without that being visible in the artifact people compare."""
    module = _load_runner(
        monkeypatch,
        tmp_path,
        BENCH_EXPERIMENT_ID="exp-approval",
        BENCH_CANDIDATE_ID="full",
        BENCH_CONFIG_FINGERPRINT="f" * 64,
        BENCH_PAIR_SEED="seed-1",
    )
    spec = module.SCENARIOS[0]

    module._record_statistical_trial(
        spec,
        _score(spec.name),
        trial_index=1,
        latency_seconds=1.0,
        trace_completeness={
            "complete": True,
            "cost_usd": 1.0,
            "spans": 4,
            "records_sha256": "c" * 64,
            "artifact_path": "reports/trace.json",
        },
        harness_approvals=1,
    )

    row = json.loads((tmp_path / "trials.jsonl").read_text().splitlines()[0])
    assert "harness_approved" in row["failure_categories"]


def test_an_unaided_trial_is_not_marked(monkeypatch, tmp_path):
    module = _load_runner(
        monkeypatch,
        tmp_path,
        BENCH_EXPERIMENT_ID="exp-approval",
        BENCH_CANDIDATE_ID="full",
        BENCH_CONFIG_FINGERPRINT="f" * 64,
        BENCH_PAIR_SEED="seed-1",
    )
    spec = module.SCENARIOS[0]

    module._record_statistical_trial(
        spec,
        _score(spec.name),
        trial_index=1,
        latency_seconds=1.0,
        trace_completeness={
            "complete": True,
            "cost_usd": 1.0,
            "spans": 4,
            "records_sha256": "c" * 64,
            "artifact_path": "reports/trace.json",
        },
    )

    row = json.loads((tmp_path / "trials.jsonl").read_text().splitlines()[0])
    assert "harness_approved" not in row["failure_categories"]


class _FakeResult:
    def __init__(self, scenario):
        self.status = "VERIFIED_RECOVERED"
        self.application_status = "pending_acknowledgment"
        self.scenario = scenario


def _score(scenario: str) -> RunScore:
    return RunScore(
        scenario=scenario,
        resolved=True,
        oracle_status="VERIFIED_RECOVERED",
        application_status="pending_acknowledgment",
        mttr_seconds=240.0,
        grader_status="PASS",
        rubric_version="v2",
        diagnosis_confidence=0.71,
        diagnosis_confidence_outcome=True,
        remediation_confidence=0.55,
        remediation_confidence_outcome=True,
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
