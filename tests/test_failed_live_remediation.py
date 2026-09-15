#!/usr/bin/env python3
"""An approved remediation whose calls all failed must not read as a fix.

Live on 2026-09-15, incident `be398969` ([pdf-thumbnailer] PodCrashLooping).
A human approved the plan in Slack. k3s had died on a Codespace resume, so
every kubectl call came back:

    executor MCP call failed: ... HTTPSConnectionPool(host='host.docker.internal',
    port=6443) ... [Errno 111] Connection refused

Three surfaces then described that run, and all three described it wrongly:

* the resolution report posted to Slack listed the failed commands under
  "**What the agent did:**" with no marker, so the thread said the memory
  limit had been raised to 256Mi — it was still 64Mi and the pod was still
  OOMKilling, 68 restarts deep;
* `compute_incident_status` returned INVESTIGATED, whose meaning is "nothing
  was changed and nothing was tried", instead of REMEDIATION_FAILED;
* `assess_learning_eligibility` graded it `dry_run` — "only dry-run actions
  were observed" — because the planning pass leaves read-only actions in
  `executed`.

The honest data existed the whole time: `act_phase.summarise_live_execution`
wrote "0/2 mutating action(s) EXECUTED [ERROR]" into the same payload. These
tests hold the three consumers to what the payload already said.
"""

from __future__ import annotations

import pytest

from backend.models import IncidentStatus
from sre_agent.incident_status import compute_incident_status
from sre_agent.resolution_report import build_resolution_report
from sre_agent.verified_learning import assess_learning_eligibility

# The shape `act_phase` produced, trimmed to what these consumers read.
CONNECTION_REFUSED = (
    "executor MCP call failed: Error executing tool patch_resource_limits: "
    "HTTPSConnectionPool(host='host.docker.internal', port=6443): Max retries "
    "exceeded (Caused by NewConnectionError(...[Errno 111] Connection refused))"
)

LIVE_RESULTS_ALL_FAILED = [
    {
        "action_type": "inspect",
        "target": "pdf-thumbnailer",
        "status": "ERROR",
        "command": "kubectl get deployment/pdf-thumbnailer -n meridian -o yaml",
        "detail": CONNECTION_REFUSED,
    },
    {
        "action_type": "config_change",
        "target": "pdf-thumbnailer",
        "status": "ERROR",
        "command": (
            "kubectl set resources deployment/pdf-thumbnailer "
            "-c pdf-thumbnailer --limits=memory=256Mi -n meridian"
        ),
        "detail": CONNECTION_REFUSED,
    },
    {
        "action_type": "escalate",
        "target": "pdf-thumbnailer service owner / platform team",
        "status": "EXECUTED",
        "command": "notify on-call: escalate (no infrastructure mutation)",
        "detail": "Paged on-call in the incident thread.",
    },
]

# The dry-run planning pass. Present on every run, no `status` field — this is
# what made the failure look like a dry run.
EXECUTED_DRY_RUN_PASS = [
    {
        "action_type": "inspect",
        "target": "pdf-thumbnailer",
        "command": "kubectl get deployment/pdf-thumbnailer -n meridian -o yaml",
    },
]


def _report(live_results, executed=None):
    return {
        "severity": "UNKNOWN",
        "plan_present": True,
        "aggregate_decision": "requires_approval",
        "approval": {"status": "approved"},
        "executed": executed if executed is not None else EXECUTED_DRY_RUN_PASS,
        "live_results": live_results,
    }


class _Alert:
    alert_name = "PodCrashLooping"
    labels = {"service": "pdf-thumbnailer"}


class _State:
    alert_context = _Alert()
    reflector_analysis = {"hypothesis": "memory limit 64Mi below startup working set"}


# ---------------------------------------------------------------------------
# The Slack report
# ---------------------------------------------------------------------------

def test_the_report_does_not_claim_a_failed_command_as_something_it_did():
    md = build_resolution_report(_State(), _report(LIVE_RESULTS_ALL_FAILED))["markdown"]

    # The heading itself has to stop claiming authorship.
    assert "**What the agent did:**" not in md
    assert "**What was attempted:**" in md

    # Every failed line is marked as failed, next to the command it names.
    failed_line = next(
        line for line in md.splitlines() if "--limits=memory=256Mi" in line
    )
    assert "❌" in failed_line and "failed" in failed_line

    # And the reason is there, so the reader can act on it.
    assert "Connection refused" in md


def test_the_report_leads_with_the_cluster_being_untouched():
    md = build_resolution_report(_State(), _report(LIVE_RESULTS_ALL_FAILED))["markdown"]

    banner = "**Nothing was changed on the cluster.**"
    assert banner in md
    # Before the command list, not buried under it.
    assert md.index(banner) < md.index("--limits=memory=256Mi")

    assert "The problem is not fixed and the cluster is untouched." in md
    assert "may need manual attention" not in md


def test_a_successful_action_is_still_marked_as_one():
    landed = [
        dict(LIVE_RESULTS_ALL_FAILED[1], status="EXECUTED", detail="patched"),
        LIVE_RESULTS_ALL_FAILED[2],
    ]
    md = build_resolution_report(_State(), _report(landed))["markdown"]

    assert "**What the agent did:**" in md
    assert "Nothing was changed on the cluster" not in md
    assert "✅" in md


def test_a_plan_that_never_ran_live_renders_as_it_always_did():
    """Back-compat: with no `live_results`, the dry-run `executed` list has no
    statuses and must not sprout markers or a failure banner."""
    md = build_resolution_report(_State(), _report([], executed=EXECUTED_DRY_RUN_PASS))[
        "markdown"
    ]

    assert "**What the agent did:**" in md
    assert "Nothing was changed on the cluster" not in md
    assert "❌" not in md


def test_a_partial_failure_still_counts_as_a_fix_landing():
    """One mutation landing is a changed cluster, whatever else failed — the
    banner would be a lie in the other direction."""
    mixed = [
        dict(LIVE_RESULTS_ALL_FAILED[1], status="EXECUTED"),
        LIVE_RESULTS_ALL_FAILED[0],
    ]
    md = build_resolution_report(_State(), _report(mixed))["markdown"]

    assert "Nothing was changed on the cluster" not in md
    assert "❌" in md  # the failed read is still marked


# ---------------------------------------------------------------------------
# The status column
# ---------------------------------------------------------------------------

def test_an_approved_run_whose_mutations_all_errored_is_a_failed_remediation():
    status = compute_incident_status(_State(), _report(LIVE_RESULTS_ALL_FAILED), None)

    assert status == IncidentStatus.REMEDIATION_FAILED


def test_a_run_that_only_read_and_paged_is_still_an_investigation():
    """The branch this fix narrows. Reads and pages changing nothing is not a
    failure — it is the agent handing the incident to a human."""
    read_only = [
        dict(LIVE_RESULTS_ALL_FAILED[0], status="EXECUTED"),
        LIVE_RESULTS_ALL_FAILED[2],
    ]

    assert compute_incident_status(_State(), _report(read_only), None) == (
        IncidentStatus.INVESTIGATED
    )


def test_a_refused_mutation_is_not_a_failed_remediation():
    """REFUSED means the executor never issued the call — policy stopped it.
    Nothing was attempted against the cluster, so it stays an investigation."""
    refused = [
        dict(LIVE_RESULTS_ALL_FAILED[1], status="REFUSED", detail="policy: blocked"),
        LIVE_RESULTS_ALL_FAILED[2],
    ]

    assert compute_incident_status(_State(), _report(refused), None) == (
        IncidentStatus.INVESTIGATED
    )


def test_a_failed_read_alone_does_not_fail_the_remediation():
    """A read erroring is not a remediation failing; only a mutating call is."""
    read_failed = [LIVE_RESULTS_ALL_FAILED[0], LIVE_RESULTS_ALL_FAILED[2]]

    assert compute_incident_status(_State(), _report(read_failed), None) == (
        IncidentStatus.INVESTIGATED
    )


# ---------------------------------------------------------------------------
# The learning record
# ---------------------------------------------------------------------------

def test_a_failed_live_run_is_not_filed_as_a_dry_run():
    eligibility = assess_learning_eligibility(
        act_report=_report(LIVE_RESULTS_ALL_FAILED),
        verification_outcome=None,
        human_approved=True,
    )

    assert eligibility.outcome_class == "failed"
    assert eligibility.dry_run_only is False
    assert not eligibility.eligible_for_success
    assert "every live mutating action failed" in eligibility.reasons


def test_a_genuine_dry_run_is_still_a_dry_run():
    eligibility = assess_learning_eligibility(
        act_report=_report([], executed=EXECUTED_DRY_RUN_PASS),
        verification_outcome=None,
        human_approved=True,
    )

    assert eligibility.outcome_class == "dry_run"
    assert eligibility.dry_run_only is True


@pytest.mark.parametrize(
    "surface",
    ["status", "report", "learning"],
    ids=["status", "report", "learning"],
)
def test_no_surface_describes_the_live_failure_as_a_success(surface):
    """One assertion per consumer, so a regression in any single one fails on
    its own name rather than hiding behind the others."""
    payload = _report(LIVE_RESULTS_ALL_FAILED)

    if surface == "status":
        assert compute_incident_status(_State(), payload, None) != (
            IncidentStatus.PENDING_ACKNOWLEDGMENT
        )
        assert compute_incident_status(_State(), payload, None) != (
            IncidentStatus.INVESTIGATED
        )
    elif surface == "report":
        built = build_resolution_report(_State(), payload)
        assert built["resolved"] is False
        assert "Nothing was changed on the cluster" in built["markdown"]
    else:
        assert not assess_learning_eligibility(
            act_report=payload, verification_outcome=None, human_approved=True
        ).eligible_for_success
