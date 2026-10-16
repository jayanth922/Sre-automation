"""Every benchmark scenario that needs a fix has a runbook that prescribes one.

With runbook-only remediation, a scenario whose alert no reviewed runbook
covers can only escalate, and its remediation criterion fails however good the
diagnosis was. Editing a runbook must not silently turn a fixable scenario into
an unfixable one, so the shipped Meridian runbooks are checked against v3.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from benchmarks.structured_grading import NON_REMEDIATION_ACTION_TYPES

from sre_agent.runbook_authority import authorize_plan, parse_runbook

REPO = Path(__file__).resolve().parents[1]
RUNBOOKS = REPO / "examples" / "meridian" / "runbooks"
V3 = REPO / "evals" / "benchmarks" / "datasets" / "v3"


def _scenarios():
    for split in ("train", "dev", "holdout"):
        yield from json.loads((V3 / f"{split}.json").read_text())["scenarios"]


def _library():
    return [parse_runbook(path.stem, path.read_text()) for path in sorted(RUNBOOKS.glob("*.md"))]


def _proposals(action_type):
    """The parameters the planner could give the action."""
    if action_type == "config_change":
        return [
            {"env": {"DB_POOL_SIZE": "40"}},
            {"memory": "1Gi"},
        ]
    return [{}]


_NEEDS_A_FIX = [
    pytest.param(s, id=s["id"])
    for s in _scenarios()
    if set(s["allowed_action_types"]) - NON_REMEDIATION_ACTION_TYPES
]


def test_v3_has_scenarios_that_need_a_fix():
    assert len(_NEEDS_A_FIX) >= 15


@pytest.mark.parametrize("scenario", _NEEDS_A_FIX)
def test_a_runbook_prescribes_an_allowed_fix(scenario):
    library = _library()
    alert = scenario["alert"]["alertname"]
    service = scenario["root_cause"]["service"]
    allowed = sorted(set(scenario["allowed_action_types"]) - NON_REMEDIATION_ACTION_TYPES)

    prescribed = [
        action_type
        for action_type in allowed
        for parameters in _proposals(action_type)
        if authorize_plan(
            [SimpleNamespace(action_type=action_type, target=service, parameters=parameters)],
            library,
            alert,
        ).authorized
    ]

    assert prescribed, (
        f"no runbook for {alert} prescribes any of {allowed} on {service}; "
        "the agent can only escalate"
    )
