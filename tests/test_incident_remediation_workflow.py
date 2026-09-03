"""Unit tests for IncidentRemediationWorkflow's non-Temporal-runtime logic:
the two approval-gate signal handlers' first-write-wins idempotency and the
deferral sentinel graph_builder.py relies on. Full @workflow.run orchestration
needs a real WorkflowEnvironment and is exercised in CI with the temporalio
extra installed, not here.

incident_remediation_workflow.py imports the `temporalio` SDK at module level
(needed for its @activity.defn/@workflow.defn decorators), an optional extra
(`pip install sre-agent[temporal]`) not installed by default — skip cleanly
rather than erroring collection when it's absent (same pattern as
test_sandbox_workflow.py).
"""

import pytest

pytest.importorskip("temporalio")

from sre_agent.incident_remediation_workflow import (  # noqa: E402
    DEFAULT_APPROVAL_TIMEOUT_SECONDS,
    DEFERRED_TO_DETERMINISTIC_PIPELINE,
    IncidentRemediationInput,
    IncidentRemediationWorkflow,
    PatchGenerationResult,
    PrResult,
    RemediationVerdict,
    _parse_verification_commands,
    _repo_clone_url,
)


def test_deferred_sentinel_is_not_a_known_autonomy_decision():
    from sre_agent.policy_gate import AutonomyDecision

    known = {d.value for d in AutonomyDecision}
    assert DEFERRED_TO_DETERMINISTIC_PIPELINE not in known


def test_incident_remediation_input_defaults():
    params = IncidentRemediationInput(
        incident_id="inc-1",
        organization_id="org-1",
        cluster_id="cluster-1",
        action_type="revert_commit",
        target="checkout-service",
        runner_image="sentinel/runner:latest",
        baseline_command=["python", "baseline.py"],
        candidate_command=["python", "candidate.py"],
        patch="diff --git a/x b/x",
        failure_signature="panic: nil pointer dereference",
    )
    assert params.repo == ""
    assert params.env == {}
    assert params.approval_timeout_seconds == DEFAULT_APPROVAL_TIMEOUT_SECONDS


def test_incident_remediation_input_patch_defaults_to_empty_for_phase_f():
    # Phase F: patch/baseline/candidate are now optional — generate_patch_activity
    # fills them in when the planner only identified a code_fix was needed.
    params = IncidentRemediationInput(
        incident_id="inc-1",
        organization_id="org-1",
        cluster_id="cluster-1",
        action_type="code_fix",
        target="checkout-service",
        runner_image="sentinel/runner:latest",
        failure_signature="panic: nil pointer dereference",
    )
    assert params.patch == ""
    assert params.baseline_command == []
    assert params.candidate_command == []
    assert params.fix_description == ""


# ── Phase F: patch-generation helpers (pure, no I/O) ─────────────────────────
def test_repo_clone_url_without_token():
    assert _repo_clone_url("org/repo") == "https://github.com/org/repo.git"


def test_repo_clone_url_embeds_token():
    url = _repo_clone_url("org/repo", "ghp_secret")
    assert url == "https://ghp_secret@github.com/org/repo.git"


def test_parse_verification_commands_from_actor_output():
    output = (
        "I fixed the nil pointer check in handler.go.\n"
        "BASELINE_COMMAND: go test ./... -run TestHandler\n"
        "CANDIDATE_COMMAND: go test ./... -run TestHandler"
    )
    baseline, candidate = _parse_verification_commands(output, [], [])
    assert baseline == ["go", "test", "./...", "-run", "TestHandler"]
    assert candidate == ["go", "test", "./...", "-run", "TestHandler"]


def test_parse_verification_commands_falls_back_when_absent():
    baseline, candidate = _parse_verification_commands(
        "no markers here", ["fallback-baseline"], ["fallback-candidate"]
    )
    assert baseline == ["fallback-baseline"]
    assert candidate == ["fallback-candidate"]


def test_patch_generation_result_defaults():
    result = PatchGenerationResult("FAILED", detail="no repo")
    assert result.patch == ""
    assert result.baseline_command == []
    assert result.candidate_command == []


def test_start_fix_signal_is_first_write_wins():
    wf = IncidentRemediationWorkflow()
    assert wf.phase() == "AWAITING_START_FIX"

    wf.decide_start_fix(True, "alice")
    assert wf._start_fix_decision is True
    assert wf._start_fix_actor == "alice"

    # A duplicate/late signal must not overwrite the first decision.
    wf.decide_start_fix(False, "bob")
    assert wf._start_fix_decision is True
    assert wf._start_fix_actor == "alice"


def test_start_fix_signal_denial_is_recorded():
    wf = IncidentRemediationWorkflow()
    wf.decide_start_fix(False, "alice")
    assert wf._start_fix_decision is False
    assert wf._start_fix_actor == "alice"


def test_raise_pr_signal_is_first_write_wins():
    wf = IncidentRemediationWorkflow()
    wf.decide_raise_pr(True, "carol")
    wf.decide_raise_pr(False, "dave")
    assert wf._raise_pr_decision is True
    assert wf._raise_pr_actor == "carol"


def test_gates_are_independent():
    wf = IncidentRemediationWorkflow()
    wf.decide_start_fix(True, "alice")
    assert wf._raise_pr_decision is None
    wf.decide_raise_pr(True, "alice")
    assert wf._start_fix_decision is True
    assert wf._raise_pr_decision is True


def test_signal_actor_defaults_to_unknown_when_blank():
    wf = IncidentRemediationWorkflow()
    wf.decide_start_fix(True, "")
    assert wf._start_fix_actor == "unknown"


def test_pr_result_and_verdict_are_plain_dataclasses():
    pr = PrResult("PR_CREATED", "PR created.", pr_url="https://github.com/org/repo/pull/1")
    assert pr.status == "PR_CREATED"
    verdict = RemediationVerdict("PR_CREATED", "done", pr_url=pr.pr_url, verification_status="RESOLVED")
    assert verdict.verification_status == "RESOLVED"
