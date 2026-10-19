#!/usr/bin/env python3
"""The executor's bound rollback: target by template hash, refuse on drift.

``rollback_deployment`` used to run ``kubectl rollout undo`` — "the previous
revision", whatever that was by the time it ran. A retry, a second approval or
an earlier restart each moved it, and undo toggles back onto the faulty
version. The pure ``rollout.py`` half is loaded directly; the tool itself runs
against a fake apiserver with the kubernetes package stubbed out, since the
test environment does not install it.
"""

import asyncio
import copy
import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

_EXECUTOR_DIR = (
    Path(__file__).resolve().parents[1]
    / "services"
    / "edge_mcp_servers"
    / "mcp_servers"
    / "executor_real"
)


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _EXECUTOR_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rollout = _load("executor_rollout", "rollout.py")
guardrails = _load("executor_rollout_guardrails", "guardrails.py")

REVISION = rollout.REVISION_ANNOTATION
RESTARTED_AT = rollout.RESTARTED_AT_ANNOTATION


def _template(image, *, restarted_at=None, template_hash=None):
    labels = {"app": "checkout"}
    if template_hash:
        labels[rollout.POD_TEMPLATE_HASH_LABEL] = template_hash
    metadata = {"labels": labels}
    if restarted_at:
        metadata["annotations"] = {RESTARTED_AT: restarted_at}
    return {
        "metadata": metadata,
        "spec": {"containers": [{"name": "checkout", "image": image}]},
    }


def _rs(template_hash, revision, image, *, restarted_at=None, owner="checkout", uid="dep-uid"):
    return {
        "metadata": {
            "name": f"{owner}-{template_hash}",
            "labels": {"app": "checkout", rollout.POD_TEMPLATE_HASH_LABEL: template_hash},
            "annotations": {REVISION: str(revision)},
            "creationTimestamp": f"2026-10-19T10:00:{revision:02d}Z",
            "ownerReferences": [
                {"kind": "Deployment", "name": owner, "uid": uid, "controller": True}
            ],
        },
        "spec": {
            "template": _template(image, restarted_at=restarted_at, template_hash=template_hash)
        },
        "status": {"replicas": 0},
    }


def _deployment(image, *, restarted_at=None, resource_version="100", paused=False):
    return {
        "metadata": {
            "name": "checkout",
            "namespace": "meridian",
            "uid": "dep-uid",
            "resourceVersion": resource_version,
            "generation": 4,
        },
        "spec": {
            "selector": {"matchLabels": {"app": "checkout"}},
            "template": _template(image, restarted_at=restarted_at),
            "paused": paused,
        },
        "status": {"observedGeneration": 4},
    }


GOOD, BAD = "checkout:v1", "checkout:v2"


def _bad_deploy_history():
    """Revision 1 good, revision 2 the bad deploy the deployment now runs."""
    return _deployment(BAD), [_rs("aaa111", 1, GOOD), _rs("bbb222", 2, BAD)]


def _restart_after_bad_deploy():
    """A rolling restart after the bad deploy: revision 3 runs what 2 ran."""
    stamp = "2026-10-19T10:05:00Z"
    return _deployment(BAD, restarted_at=stamp), [
        _rs("aaa111", 1, GOOD),
        _rs("bbb222", 2, BAD),
        _rs("ccc333", 3, BAD, restarted_at=stamp),
    ]


# -- pure history -------------------------------------------------------------


def test_current_replica_set_matches_template_ignoring_hash_label():
    deployment, replica_sets = _bad_deploy_history()
    state = rollout.rollout_state(deployment, replica_sets)
    assert state["current_template_hash"] == "bbb222"
    assert state["current_revision"] == 2
    assert [r["template_hash"] for r in state["revisions"]] == ["bbb222", "aaa111"]
    assert state["revisions"][1]["images"] == {"checkout": GOOD}


def test_a_restart_only_revision_shares_its_predecessors_fingerprint():
    deployment, replica_sets = _restart_after_bad_deploy()
    state = rollout.rollout_state(deployment, replica_sets)
    by_hash = {r["template_hash"]: r for r in state["revisions"]}
    assert state["current_template_hash"] == "ccc333"
    assert by_hash["ccc333"]["fingerprint"] == by_hash["bbb222"]["fingerprint"]
    assert by_hash["aaa111"]["fingerprint"] != by_hash["bbb222"]["fingerprint"]
    assert state["current_fingerprint"] == by_hash["ccc333"]["fingerprint"]


def test_a_template_no_replica_set_matches_yet_is_unknown_not_current():
    deployment, replica_sets = _bad_deploy_history()
    deployment["spec"]["template"] = _template("checkout:v3")
    assert rollout.rollout_state(deployment, replica_sets)["current_template_hash"] is None


def test_another_deployments_replica_sets_are_never_candidates():
    deployment, replica_sets = _bad_deploy_history()
    foreign = _rs("ddd444", 9, GOOD, owner="checkout-canary", uid="other-uid")
    state = rollout.rollout_state(deployment, replica_sets + [foreign])
    assert "ddd444" not in {r["template_hash"] for r in state["revisions"]}
    verdict, _ = rollout.classify_rollback(deployment, replica_sets + [foreign], "ddd444", "bbb222")
    assert verdict == rollout.TARGET_MISSING


def test_empty_and_absent_fields_compare_equal():
    deployment, replica_sets = _bad_deploy_history()
    deployment["spec"]["template"]["metadata"]["annotations"] = {}
    deployment["spec"]["template"]["spec"]["volumes"] = []
    assert rollout.rollout_state(deployment, replica_sets)["current_template_hash"] == "bbb222"


# -- pure classification ------------------------------------------------------


def test_on_the_expected_template_the_rollback_applies():
    deployment, replica_sets = _bad_deploy_history()
    verdict, _ = rollout.classify_rollback(deployment, replica_sets, "aaa111", "bbb222")
    assert verdict == rollout.APPLY


def test_already_on_the_target_needs_no_write():
    deployment, replica_sets = _bad_deploy_history()
    deployment["spec"]["template"] = _template(GOOD)
    verdict, _ = rollout.classify_rollback(deployment, replica_sets, "aaa111", "bbb222")
    assert verdict == rollout.ALREADY_AT_TARGET


def test_a_restart_after_binding_is_drift_not_a_match():
    """Same images, new template hash: not the state the approver saw."""
    _, replica_sets = _restart_after_bad_deploy()
    deployment, _ = _restart_after_bad_deploy()
    verdict, reason = rollout.classify_rollback(deployment, replica_sets, "aaa111", "bbb222")
    assert verdict == rollout.DRIFT
    assert "ccc333" in reason


def test_a_template_change_the_controller_has_not_picked_up_is_drift():
    deployment, replica_sets = _bad_deploy_history()
    deployment["spec"]["template"] = _template("checkout:v3")
    verdict, reason = rollout.classify_rollback(deployment, replica_sets, "aaa111", "bbb222")
    assert verdict == rollout.DRIFT
    assert "no ReplicaSet matches" in reason


def test_a_pruned_target_is_refused():
    deployment, replica_sets = _bad_deploy_history()
    verdict, _ = rollout.classify_rollback(deployment, replica_sets[1:], "aaa111", "bbb222")
    assert verdict == rollout.TARGET_MISSING


def test_a_paused_rollout_is_refused():
    deployment, replica_sets = _bad_deploy_history()
    deployment["spec"]["paused"] = True
    verdict, _ = rollout.classify_rollback(deployment, replica_sets, "aaa111", "bbb222")
    assert verdict == rollout.PAUSED


def test_the_patch_is_a_compare_and_swap_on_resource_version():
    deployment, replica_sets = _bad_deploy_history()
    target = rollout.find_replica_set(deployment, replica_sets, "aaa111")
    patch = rollout.rollback_patch(deployment, target)
    assert patch[0] == {"op": "test", "path": "/metadata/resourceVersion", "value": "100"}
    assert patch[1]["op"] == "replace" and patch[1]["path"] == "/spec/template"
    assert patch[1]["value"] == _template(GOOD)
    assert rollout.POD_TEMPLATE_HASH_LABEL in target["spec"]["template"]["metadata"]["labels"]


# -- guardrail ----------------------------------------------------------------


@pytest.mark.parametrize(
    "to_hash, expected_hash",
    [("", "bbb222"), ("aaa111", None), ("AAA111", "bbb222"), ("aaa111", "aaa111"), ("a b", "bbb222")],
)
def test_a_rollback_without_two_distinct_template_hashes_is_refused(monkeypatch, to_hash, expected_hash):
    monkeypatch.setenv("EXECUTOR_ALLOWED_NAMESPACES", "meridian")
    ok, _ = guardrails.guardrail_check(
        "rollback", "meridian",
        {"to_template_hash": to_hash, "expected_template_hash": expected_hash},
    )
    assert ok is False


def test_a_bound_rollback_passes_the_guardrail(monkeypatch):
    monkeypatch.setenv("EXECUTOR_ALLOWED_NAMESPACES", "meridian")
    ok, _ = guardrails.guardrail_check(
        "rollback", "meridian", {"to_template_hash": "aaa111", "expected_template_hash": "bbb222"}
    )
    assert ok is True
    assert guardrails.guardrail_check("get_rollout_state", "meridian")[0] is True
    assert guardrails.guardrail_check("get_rollout_state", "kube-system")[0] is False


# -- the tool, against a fake apiserver ---------------------------------------


class _ApiException(Exception):
    def __init__(self, status=None, reason=None):
        super().__init__(reason)
        self.status = status
        self.reason = reason


class FakeApps:
    """Raw-JSON reads and a JSON patch whose ``test`` op really tests."""

    def __init__(self, deployment, replica_sets, *, before_patch=None):
        self.deployment = deployment
        self.replica_sets = replica_sets
        self.before_patch = before_patch
        self.patches = []
        self.reads = 0

    def _bump(self):
        rv = int(self.deployment["metadata"]["resourceVersion"]) + 1
        self.deployment["metadata"]["resourceVersion"] = str(rv)

    def read_namespaced_deployment(self, name, namespace, _preload_content=True):
        self.reads += 1
        return SimpleNamespace(data=json.dumps(self.deployment).encode())

    def list_namespaced_replica_set(self, namespace, label_selector="", _preload_content=True):
        assert label_selector == "app=checkout"
        return SimpleNamespace(data=json.dumps({"items": self.replica_sets}).encode())

    def patch_namespaced_deployment(self, name, namespace, body, **kwargs):
        self.patches.append((copy.deepcopy(body), kwargs))
        if self.before_patch:
            hook, self.before_patch = self.before_patch, None
            hook(self)
        test, replace = body
        if test["value"] != self.deployment["metadata"]["resourceVersion"]:
            raise _ApiException(422, "Unprocessable Entity")
        if kwargs.get("dry_run") == "All":
            return None
        self.deployment["spec"]["template"] = copy.deepcopy(replace["value"])
        self._bump()
        return None


@pytest.fixture
def server(monkeypatch):
    kubernetes = types.ModuleType("kubernetes")
    client = types.ModuleType("kubernetes.client")
    rest = types.ModuleType("kubernetes.client.rest")
    config = types.ModuleType("kubernetes.config")
    rest.ApiException = _ApiException
    client.rest = rest
    for attr in ("AppsV1Api", "CoreV1Api", "ApiClient", "Configuration"):
        setattr(client, attr, object)
    kubernetes.client = client
    kubernetes.config = config
    for name, module in (
        ("kubernetes", kubernetes),
        ("kubernetes.client", client),
        ("kubernetes.client.rest", rest),
        ("kubernetes.config", config),
        ("rollout", rollout),
        ("guardrails", guardrails),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setenv("EXECUTOR_ALLOWED_NAMESPACES", "meridian")
    spec = importlib.util.spec_from_file_location("executor_real_server", _EXECUTOR_DIR / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rollback(server, api, **overrides):
    server._apps_api = lambda: api
    args = {
        "name": "checkout",
        "namespace": "meridian",
        "to_template_hash": "aaa111",
        "expected_template_hash": "bbb222",
        "dry_run": False,
        **overrides,
    }
    return json.loads(asyncio.run(server.rollback_deployment(**args)))


def test_tool_rolls_back_to_the_named_template_with_a_cas(server):
    api = FakeApps(*_bad_deploy_history())
    out = _rollback(server, api)
    assert out["status"] == "OK" and out["applied"] is True
    assert len(api.patches) == 1
    body, kwargs = api.patches[0]
    assert body[0]["value"] == "100" and kwargs == {}
    assert api.deployment["spec"]["template"] == _template(GOOD)
    assert out["kubectl_equivalent"] == rollout.rollback_command(
        "checkout", "meridian", "aaa111", "bbb222"
    )


def test_tool_second_run_finds_the_target_and_writes_nothing(server):
    api = FakeApps(*_bad_deploy_history())
    _rollback(server, api)
    again = _rollback(server, api)
    assert again["status"] == "ALREADY_AT_TARGET"
    assert again["applied"] is False and again["mutated"] is False
    assert len(api.patches) == 1


def test_tool_refuses_drift_without_writing(server):
    deployment, replica_sets = _restart_after_bad_deploy()
    api = FakeApps(deployment, replica_sets)
    out = _rollback(server, api)
    assert out["status"] == "STATE_DRIFT" and out["refusal_code"] == "state_drift"
    assert out["current_template_hash"] == "ccc333"
    assert api.patches == []


def test_tool_rereads_after_losing_a_status_only_race(server):
    """A status write bumps resourceVersion; the template is still expected."""
    api = FakeApps(*_bad_deploy_history(), before_patch=FakeApps._bump)
    out = _rollback(server, api)
    assert out["status"] == "OK"
    assert len(api.patches) == 2
    assert api.patches[1][0][0]["value"] == "101"
    assert api.deployment["spec"]["template"] == _template(GOOD)


def test_tool_rereads_after_losing_a_race_to_a_restart_and_refuses(server):
    def restart(api):
        api.deployment["spec"]["template"] = _template(BAD, restarted_at="2026-10-19T10:06:00Z")
        api.replica_sets.append(_rs("ccc333", 3, BAD, restarted_at="2026-10-19T10:06:00Z"))
        api._bump()

    api = FakeApps(*_bad_deploy_history(), before_patch=restart)
    out = _rollback(server, api)
    assert out["status"] == "STATE_DRIFT"
    assert len(api.patches) == 1
    assert api.deployment["spec"]["template"]["spec"]["containers"][0]["image"] == BAD


def test_tool_dry_run_validates_server_side_and_persists_nothing(server):
    api = FakeApps(*_bad_deploy_history())
    out = _rollback(server, api, dry_run=True)
    assert out["status"] == "DRY_RUN" and out["applied"] is False
    assert api.patches[0][1] == {"dry_run": "All"}
    assert api.deployment["spec"]["template"] == _template(BAD)


def test_tool_refuses_a_relative_rollback_before_reading(server):
    api = FakeApps(*_bad_deploy_history())
    out = _rollback(server, api, to_template_hash="")
    assert out["status"] == "REFUSED"
    assert api.reads == 0 and api.patches == []


def test_rollout_state_tool_is_a_read(server):
    api = FakeApps(*_restart_after_bad_deploy())
    server._apps_api = lambda: api
    out = json.loads(asyncio.run(server.get_rollout_state(name="checkout", namespace="meridian")))
    assert out["status"] == "OK" and out["mutated"] is False
    assert out["current_template_hash"] == "ccc333"
    assert api.patches == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
