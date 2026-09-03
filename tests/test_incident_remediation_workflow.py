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
    PrResult,
    RemediationVerdict,
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
