#!/usr/bin/env python3
"""#71: a missing incident must be explained from evidence, never guessed.

The harness fires the scenario's alert itself and the platform opens the
incident synchronously inside that webhook request, so "no incident appeared"
is never "the incident was slow". It means the alert was absorbed -- deduped
onto an identical open title, or folded into an open incident for the same
service -- or that it vanished inside the platform. The harness could not tell
these apart: it printed one fixed sentence blaming dedup, having checked
nothing, and threw away the webhook's own response body, which is the
authoritative record of what happened to that alert.

The cost was a wrong finding in a release artifact. When
`checkout_memory_leak_oom` produced no incident in both arms of the 2026-09-25
campaign, `reports/ablation-20260925/ATTESTATION.md` recorded a memory-threshold
explanation as "root cause proven, not inferred". That explanation cannot be
right -- the Prometheus rule's threshold has no say in whether the harness's own
synthetic alert opens an incident -- and the real cause went unrecorded.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

BENCHMARKS = Path(__file__).resolve().parents[1] / "evals" / "benchmarks"


@pytest.fixture(scope="module")
def bench():
    spec = importlib.util.spec_from_file_location(
        "sre_bench_diagnosis_under_test", BENCHMARKS / "sre_bench.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


CREDS = SimpleNamespace(
    base_url="http://platform.test",
    cluster_id="cluster-1",
    cluster_token="token-1",
)


class _Jwt:
    async def headers(self):
        return {"Authorization": "Bearer test"}


class _Response:
    def __init__(self, payload, *, raises=None):
        self._payload = payload
        self._raises = raises

    def raise_for_status(self):
        return None

    def json(self):
        if self._raises is not None:
            raise self._raises
        return self._payload


class _Client:
    """Serves one incident list and records the webhook body it was posted."""

    def __init__(self, incidents=(), webhook_body=None, get_raises=None):
        self.incidents = list(incidents)
        self.webhook_body = webhook_body
        self.get_raises = get_raises
        self.posted = []

    async def get(self, url, headers=None):
        if self.get_raises is not None:
            raise self.get_raises
        return _Response(self.incidents)

    async def post(self, url, json=None, headers=None):
        self.posted.append((url, json))
        return _Response(self.webhook_body)


def _spec(bench, *, alertname="CheckoutMemoryApproachingLimit",
          service="checkout-service"):
    from scoring import ScenarioSpec  # noqa: PLC0415

    return ScenarioSpec(
        name="checkout_memory_leak_oom",
        alert={
            "alertname": alertname,
            "service": service,
            "severity": "warning",
            "summary": "heap climbing",
            "description": "heap climbing on checkout",
        },
        ground_truth_service=service,
        root_cause_keywords=["memory", "leak"],
        expected_action_types={"restart_deployment"},
        expected_severity_band={"SEV2", "SEV3"},
        recovery_probe=object(),
        unsafe_action_types={"delete_namespace"},
        dataset_version="sentinel-sre-v2",
        scenario_version="1.0.0",
        taxonomy={"category": "resource", "fault_mode": "memory_leak"},
    )


def _incident(bench, *, ident, title, status="investigating", minutes_ago=8):
    created = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return {
        "id": ident,
        "title": title,
        "status": status,
        "created_at": created.isoformat(),
    }


# --------------------------------------------------------------- the receipt


def test_the_webhook_receipt_is_kept_instead_of_discarded(bench):
    """The authoritative answer travels back in the response body."""
    client = _Client(
        webhook_body={
            "received": 1,
            "incidents_created": 0,
            "incidents_folded": 1,
            "resolved_reconciled": 0,
        }
    )
    receipt = _run(
        bench._fire_alert(client, _spec(bench), datetime.now(timezone.utc), CREDS)
    )
    assert receipt.created == 0
    assert receipt.folded == 1
    assert receipt.absorbed is True
    assert client.posted, "the alert must still be delivered"


def test_a_created_incident_is_not_absorbed(bench):
    client = _Client(webhook_body={"incidents_created": 1, "incidents_folded": 0})
    receipt = _run(
        bench._fire_alert(client, _spec(bench), datetime.now(timezone.utc), CREDS)
    )
    assert receipt.absorbed is False


def test_an_unreadable_body_does_not_fail_an_accepted_stimulus(bench):
    """The receipt is diagnostic. A platform that accepted the alert has not
    failed the trial just because its body did not parse."""

    class _BadClient(_Client):
        async def post(self, url, json=None, headers=None):
            return _Response(None, raises=ValueError("not json"))

    receipt = _run(
        bench._fire_alert(_BadClient(), _spec(bench), datetime.now(timezone.utc), CREDS)
    )
    assert receipt.created == 0
    assert receipt.raw == {}


# ------------------------------------------------------------- the diagnosis


def test_an_identical_open_title_is_named_as_the_dedup_target(bench):
    absorber = _incident(
        bench,
        ident="aaaaaaaa-1111-2222-3333-444444444444",
        title="[checkout-service] CheckoutMemoryApproachingLimit",
    )
    status, reason = _run(
        bench._diagnose_missing_incident(
            _Client([absorber]), _Jwt(), CREDS, _spec(bench),
            bench.AlertReceipt(created=0, folded=0, reconciled=0, raw={}),
        )
    )
    assert status == "incident_absorbed"
    assert "deduped" in reason
    assert "aaaaaaaa" in reason, "the absorbing incident must be named"


def test_an_open_same_service_incident_is_named_as_the_fold_target(bench):
    """The 2026-09-25 shape: a different alert on the same service, still open
    inside the fold window, takes the alert and no new incident appears."""
    absorber = _incident(
        bench,
        ident="5a90bd14-0000-0000-0000-000000000000",
        title="[checkout-service] CheckoutHighErrorRate",
        minutes_ago=8,
    )
    status, reason = _run(
        bench._diagnose_missing_incident(
            _Client([absorber]), _Jwt(), CREDS, _spec(bench),
            bench.AlertReceipt(created=0, folded=1, reconciled=0, raw={}),
        )
    )
    assert status == "incident_absorbed"
    assert "folded" in reason
    assert "5a90bd14" in reason
    assert "CheckoutHighErrorRate" in reason
    assert "folded=1" in reason, "the receipt counts belong in the record"


def test_a_resolved_incident_absorbs_nothing(bench):
    """Teardown closing the previous scenario's incident is the #66 fix working;
    a closed incident must not then be blamed for the next one."""
    closed = _incident(
        bench,
        ident="deadbeef-0000-0000-0000-000000000000",
        title="[checkout-service] CheckoutHighErrorRate",
        status="resolved",
    )
    status, reason = _run(
        bench._diagnose_missing_incident(
            _Client([closed]), _Jwt(), CREDS, _spec(bench),
            bench.AlertReceipt(created=0, folded=0, reconciled=0, raw={}),
        )
    )
    assert status == "incident_not_created"
    assert "deadbeef" not in reason


def test_an_incident_outside_the_fold_window_absorbs_nothing(bench):
    stale = _incident(
        bench,
        ident="cafebabe-0000-0000-0000-000000000000",
        title="[checkout-service] CheckoutHighErrorRate",
        minutes_ago=bench.FOLD_WINDOW_MINUTES + 5,
    )
    status, reason = _run(
        bench._diagnose_missing_incident(
            _Client([stale]), _Jwt(), CREDS, _spec(bench),
            bench.AlertReceipt(created=0, folded=0, reconciled=0, raw={}),
        )
    )
    assert status == "incident_not_created"
    assert "cafebabe" not in reason


def test_another_services_incident_absorbs_nothing(bench):
    other = _incident(
        bench,
        ident="0ea4968d-0000-0000-0000-000000000000",
        title="[payment-service] PaymentServiceHighErrorRate",
    )
    status, _ = _run(
        bench._diagnose_missing_incident(
            _Client([other]), _Jwt(), CREDS, _spec(bench),
            bench.AlertReceipt(created=0, folded=0, reconciled=0, raw={}),
        )
    )
    assert status == "incident_not_created"


def test_a_fold_the_harness_cannot_name_is_still_reported_as_a_fold(bench):
    """The receipt outranks the harness's own reconstruction: if the platform
    says it folded, the trial was absorbed even when the target has since
    closed and cannot be pointed at."""
    status, reason = _run(
        bench._diagnose_missing_incident(
            _Client([]), _Jwt(), CREDS, _spec(bench),
            bench.AlertReceipt(created=0, folded=1, reconciled=0, raw={}),
        )
    )
    assert status == "incident_absorbed"
    assert "no longer open" in reason


def test_nothing_accounting_for_it_is_said_plainly(bench):
    status, reason = _run(
        bench._diagnose_missing_incident(
            _Client([]), _Jwt(), CREDS, _spec(bench),
            bench.AlertReceipt(created=0, folded=0, reconciled=0, raw={}),
        )
    )
    assert status == "incident_not_created"
    assert "vanished" in reason
    assert "created=0" in reason


def test_an_unreadable_incident_list_is_reported_not_raised(bench):
    status, reason = _run(
        bench._diagnose_missing_incident(
            _Client(get_raises=RuntimeError("boom")), _Jwt(), CREDS, _spec(bench),
            bench.AlertReceipt(created=0, folded=0, reconciled=0, raw={}),
        )
    )
    assert status == "incident_not_created"
    assert "RuntimeError" in reason and "boom" in reason


def test_different_causes_no_longer_produce_the_same_sentence(bench):
    """The defect in one line: every one of these situations used to print the
    same fixed claim about dedup. They are four different things."""
    receipt = bench.AlertReceipt(created=0, folded=0, reconciled=0, raw={})
    situations = {
        "dedup": _Client([
            _incident(bench, ident="aaaaaaaa-0000-0000-0000-000000000000",
                      title="[checkout-service] CheckoutMemoryApproachingLimit")
        ]),
        "fold": _Client([
            _incident(bench, ident="5a90bd14-0000-0000-0000-000000000000",
                      title="[checkout-service] CheckoutHighErrorRate")
        ]),
        "nothing": _Client([]),
        "unreadable": _Client(get_raises=RuntimeError("boom")),
    }
    reasons = {}
    for label, client in situations.items():
        _, reason = _run(
            bench._diagnose_missing_incident(
                client, _Jwt(), CREDS, _spec(bench), receipt
            )
        )
        reasons[label] = reason
        assert "would have deduped it away" not in reason

    assert len(set(reasons.values())) == len(reasons), (
        f"distinct causes must read differently, got {reasons}"
    )
    assert "5a90bd14" in reasons["fold"]
    assert "aaaaaaaa" in reasons["dedup"]


# ------------------------------------------- the new status reaches both sites


def test_an_absorbed_alert_is_a_platform_failure_not_an_agent_failure(bench):
    from scoring import PLATFORM_FAILURE_STATUSES  # noqa: PLC0415

    assert "incident_absorbed" in PLATFORM_FAILURE_STATUSES

    score = SimpleNamespace(
        oracle_status="INVALID_SCENARIO",
        resolved=False,
        false_resolved=False,
        application_status="incident_absorbed",
        grader_status="INCOMPLETE",
        safety_ok=True,
    )
    assert "platform_failure" in bench._failure_categories(score)


def test_both_branch_sites_read_one_shared_set(bench):
    """`score_run` and `_failure_categories` used to carry separate literal
    sets. A status added to one and missed by the other grades a harness
    failure as an agent failure, silently."""
    import scoring  # noqa: PLC0415

    bench_source = (BENCHMARKS / "sre_bench.py").read_text()
    scoring_source = (BENCHMARKS / "scoring.py").read_text()
    for source in (bench_source, scoring_source):
        assert 'in {\n        "incident_not_created",' not in source
    assert bench_source.count("PLATFORM_FAILURE_STATUSES") >= 2
    assert scoring.PLATFORM_FAILURE_STATUSES is not None


# --------------------------------------------------------------------- helper


def _run(coro):
    import asyncio  # noqa: PLC0415

    return asyncio.run(coro)
