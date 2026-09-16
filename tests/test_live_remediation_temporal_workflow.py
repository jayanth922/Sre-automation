"""Process-death coverage for the live ACT/Temporal checkpoint boundary.

The first worker is stopped after action 0 is durably checkpointed and before
action 1 begins. A replacement worker must resume at action 1: completed action
0 may not be replayed. The incident is externally cleared while no worker owns
the workflow, so the replacement must refuse action 1 and never schedule 2.
"""

import asyncio
import uuid

import pytest

pytest.importorskip("temporalio")

from temporalio import activity  # noqa: E402
from temporalio.exceptions import ApplicationError  # noqa: E402
from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

from sre_agent.incident_remediation_workflow import (  # noqa: E402
    LiveRemediationInput,
    LiveRemediationWorkflow,
)


def _request(index: int) -> dict:
    return {
        "action_index": index,
        "action": {
            "action_type": "restart",
            "target": f"service-{index}",
            "parameters": {"namespace": "demo-app"},
            "safety_check": "test",
            "rollback_plan": "test",
        },
    }


@pytest.mark.asyncio
async def test_worker_death_does_not_replay_success_and_clear_stops_later_actions():
    first_action_returned = asyncio.Event()
    invocations = []
    external_mutations = []
    incident = {"resolved": False}

    @activity.defn(name="execute_live_action_activity")
    async def first_worker_activity(params, request):
        index = int(request["action_index"])
        invocations.append(("first-worker", index))
        activity.heartbeat(f"worker-one-action-{index}")
        if index == 0:
            external_mutations.append(index)
            first_action_returned.set()
            return {
                "action_type": "restart",
                "target": "service-0",
                "status": "EXECUTED",
                "command": "restart service-0",
                "detail": "done",
            }
        raise AssertionError("worker one must die before action 1 is scheduled")

    @activity.defn(name="execute_live_action_activity")
    async def replacement_worker_activity(params, request):
        index = int(request["action_index"])
        invocations.append(("replacement-worker", index))
        # Task #40: an external clear removes remediation authority. The real
        # activity gets this result from mutation_gateway's locked incident
        # re-read before it reaches the external tool.
        assert incident["resolved"] is True
        return {
            "action_type": "restart",
            "target": f"service-{index}",
            "status": "REFUSED",
            "command": "",
            "detail": "incident_resolved: source alert cleared",
            "rejection_code": "incident_resolved",
        }

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"live-remediation-{uuid.uuid4().hex}"
        worker_one = Worker(
            env.client,
            task_queue=task_queue,
            workflows=[LiveRemediationWorkflow],
            activities=[first_worker_activity],
            max_cached_workflows=0,
        )
        worker_one_task = asyncio.create_task(worker_one.run())
        handle = await env.client.start_workflow(
            LiveRemediationWorkflow.run,
            LiveRemediationInput(
                incident_id="incident-1",
                organization_id="org-1",
                cluster_id="cluster-1",
                action_requests=[_request(0), _request(1), _request(2)],
                inter_action_delay_seconds=2,
            ),
            id=f"wf-{uuid.uuid4().hex}",
            task_queue=task_queue,
        )

        await asyncio.wait_for(first_action_returned.wait(), timeout=10)
        for _ in range(100):
            if await handle.query(LiveRemediationWorkflow.phase) == "CHECKPOINTED_ACTION_0":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("action 0 completion never reached workflow history")
        await worker_one.shutdown()
        await asyncio.wait_for(worker_one_task, timeout=10)
        incident["resolved"] = True

        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[LiveRemediationWorkflow],
            activities=[replacement_worker_activity],
            max_cached_workflows=0,
        ):
            result = await asyncio.wait_for(handle.result(), timeout=20)

    assert result.status == "SUPPRESSED_ALERT_CLEARED"
    assert external_mutations == [0]
    assert invocations.count(("first-worker", 0)) == 1
    assert ("replacement-worker", 0) not in invocations
    assert ("replacement-worker", 1) in invocations
    assert not any(index == 2 for _, index in invocations)


@pytest.mark.asyncio
async def test_pre_dispatch_failure_retries_then_mutates_once():
    attempts = []
    external_mutations = []

    @activity.defn(name="execute_live_action_activity")
    async def retryable_activity(params, request):
        index = int(request["action_index"])
        attempts.append(index)
        if len(attempts) == 1:
            raise ApplicationError(
                "setup unavailable", type="LiveActionPreDispatchError"
            )
        external_mutations.append(index)
        return {
            "action_type": "restart",
            "target": f"service-{index}",
            "status": "EXECUTED",
            "command": f"restart service-{index}",
            "detail": "done",
        }

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"live-remediation-retry-{uuid.uuid4().hex}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[LiveRemediationWorkflow],
            activities=[retryable_activity],
        ):
            result = await env.client.execute_workflow(
                LiveRemediationWorkflow.run,
                LiveRemediationInput(
                    incident_id="incident-1",
                    organization_id="org-1",
                    cluster_id="cluster-1",
                    action_requests=[_request(0)],
                ),
                id=f"wf-{uuid.uuid4().hex}",
                task_queue=task_queue,
            )

    assert result.status == "COMPLETED"
    assert attempts == [0, 0]
    assert external_mutations == [0]


@pytest.mark.asyncio
async def test_ambiguous_outcome_is_not_retried_and_stops_later_actions():
    invocations = []

    @activity.defn(name="execute_live_action_activity")
    async def ambiguous_activity(params, request):
        index = int(request["action_index"])
        invocations.append(index)
        return {
            "action_type": "restart",
            "target": f"service-{index}",
            "status": "ERROR",
            "command": f"restart service-{index}",
            "detail": "connection lost after dispatch",
            "failure_class": "outcome_unknown",
            "manual_review_required": True,
        }

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"live-remediation-ambiguous-{uuid.uuid4().hex}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[LiveRemediationWorkflow],
            activities=[ambiguous_activity],
        ):
            result = await env.client.execute_workflow(
                LiveRemediationWorkflow.run,
                LiveRemediationInput(
                    incident_id="incident-1",
                    organization_id="org-1",
                    cluster_id="cluster-1",
                    action_requests=[_request(0), _request(1)],
                ),
                id=f"wf-{uuid.uuid4().hex}",
                task_queue=task_queue,
            )

    assert result.status == "MANUAL_REVIEW_REQUIRED"
    assert invocations == [0]


@pytest.mark.asyncio
async def test_pre_dispatch_retries_are_bounded_and_stop_the_plan():
    attempts = []

    @activity.defn(name="execute_live_action_activity")
    async def unavailable_activity(params, request):
        attempts.append(int(request["action_index"]))
        raise ApplicationError(
            "setup unavailable", type="LiveActionPreDispatchError"
        )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"live-remediation-exhausted-{uuid.uuid4().hex}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[LiveRemediationWorkflow],
            activities=[unavailable_activity],
        ):
            result = await env.client.execute_workflow(
                LiveRemediationWorkflow.run,
                LiveRemediationInput(
                    incident_id="incident-1",
                    organization_id="org-1",
                    cluster_id="cluster-1",
                    action_requests=[_request(0), _request(1)],
                ),
                id=f"wf-{uuid.uuid4().hex}",
                task_queue=task_queue,
            )

    assert result.status == "MANUAL_REVIEW_REQUIRED"
    assert result.live_results[0]["failure_class"] == "pre_dispatch_retries_exhausted"
    assert attempts == [0, 0, 0]
