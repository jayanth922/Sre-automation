#!/usr/bin/env python3
"""Organization-ownership regression tests for T04."""

import asyncio
import ast
import importlib.util
import sys
import types
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest


_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class _Condition:
    model: type
    field: str
    expected: object


class _Field:
    def __init__(self, model: type, name: str):
        self.model = model
        self.name = name

    def __eq__(self, other):
        return _Condition(self.model, self.name, other)


def _model(name: str, fields: tuple[str, ...]):
    model = type(name, (), {})
    for field in fields:
        setattr(model, field, _Field(model, field))
    return model


class _Select:
    def __init__(self, model):
        self.model = model
        self.conditions = []

    def join(self, *_args, **_kwargs):
        return self

    def where(self, *conditions):
        self.conditions.extend(conditions)
        return self


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def first(self):
        return self.value


class _Result:
    def __init__(self, value):
        self.value = value

    def scalars(self):
        return _ScalarResult(self.value)


class _FakeDb:
    def __init__(self, models, clusters, incidents, slos):
        self.models = models
        self.rows = {
            models.Cluster: clusters,
            models.Incident: incidents,
            models.SLO: slos,
        }
        self.clusters = {row.id: row for row in clusters}

    def _value(self, selected_model, row, condition):
        if condition.model is selected_model:
            return getattr(row, condition.field)
        if condition.model is self.models.Cluster:
            cluster = self.clusters.get(row.cluster_id)
            return getattr(cluster, condition.field) if cluster else None
        raise AssertionError("unexpected model in ownership predicate")

    async def execute(self, statement):
        for row in self.rows[statement.model]:
            if all(
                self._value(statement.model, row, condition) == condition.expected
                for condition in statement.conditions
            ):
                return _Result(row)
        return _Result(None)


@pytest.fixture
def ownership_module(monkeypatch):
    """Load ownership.py with tiny framework stubs for the lightweight suite."""
    cluster = _model("Cluster", ("id", "org_id"))
    incident = _model("Incident", ("id", "cluster_id"))
    slo = _model("SLO", ("id", "cluster_id"))
    user = _model("User", ("id", "org_id"))
    model_module = types.ModuleType("backend.models")
    model_module.Cluster = cluster
    model_module.Incident = incident
    model_module.SLO = slo
    model_module.User = user

    database_module = types.ModuleType("backend.database")
    database_module.get_db = lambda: None
    backend_module = types.ModuleType("backend")
    backend_module.database = database_module
    backend_module.models = model_module

    class HTTPException(Exception):
        def __init__(self, status_code, detail):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    fastapi_module = types.ModuleType("fastapi")
    fastapi_module.Depends = lambda dependency: SimpleNamespace(dependency=dependency)
    fastapi_module.HTTPException = HTTPException
    fastapi_module.status = SimpleNamespace(HTTP_404_NOT_FOUND=404)

    sqlalchemy_module = types.ModuleType("sqlalchemy")
    sqlalchemy_module.select = lambda model: _Select(model)
    sqlalchemy_ext_module = types.ModuleType("sqlalchemy.ext")
    sqlalchemy_asyncio_module = types.ModuleType("sqlalchemy.ext.asyncio")
    sqlalchemy_asyncio_module.AsyncSession = type("AsyncSession", (), {})

    auth_module = types.ModuleType("sre_agent.api.v1.auth_deps")
    auth_module.get_current_user_and_org = lambda: None

    stubs = {
        "backend": backend_module,
        "backend.database": database_module,
        "backend.models": model_module,
        "fastapi": fastapi_module,
        "sqlalchemy": sqlalchemy_module,
        "sqlalchemy.ext": sqlalchemy_ext_module,
        "sqlalchemy.ext.asyncio": sqlalchemy_asyncio_module,
        "sre_agent.api.v1.auth_deps": auth_module,
    }
    for name, module in stubs.items():
        monkeypatch.setitem(sys.modules, name, module)

    spec = importlib.util.spec_from_file_location(
        "_t04_ownership",
        _ROOT / "sre_agent" / "api" / "v1" / "ownership.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module._test_models = model_module
    module._test_http_exception = HTTPException
    return module


@pytest.fixture
def two_orgs(ownership_module):
    models = ownership_module._test_models
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    cluster_a = SimpleNamespace(id=uuid.uuid4(), org_id=org_a)
    cluster_b = SimpleNamespace(id=uuid.uuid4(), org_id=org_b)
    incident_a = SimpleNamespace(id=uuid.uuid4(), cluster_id=cluster_a.id)
    incident_b = SimpleNamespace(id=uuid.uuid4(), cluster_id=cluster_b.id)
    slo_a = SimpleNamespace(id=uuid.uuid4(), cluster_id=cluster_a.id)
    slo_b = SimpleNamespace(id=uuid.uuid4(), cluster_id=cluster_b.id)
    return SimpleNamespace(
        user_a=SimpleNamespace(org_id=org_a),
        user_b=SimpleNamespace(org_id=org_b),
        cluster_a=cluster_a,
        cluster_b=cluster_b,
        incident_a=incident_a,
        incident_b=incident_b,
        slo_a=slo_a,
        slo_b=slo_b,
        db=_FakeDb(
            models,
            [cluster_a, cluster_b],
            [incident_a, incident_b],
            [slo_a, slo_b],
        ),
    )


@pytest.mark.parametrize("resource", ["cluster", "incident", "slo"])
def test_owner_can_load_resource(ownership_module, two_orgs, resource):
    if resource == "cluster":
        call = ownership_module.get_owned_cluster(
            two_orgs.cluster_a.id, two_orgs.user_a, two_orgs.db
        )
        expected = two_orgs.cluster_a
    elif resource == "incident":
        call = ownership_module.get_owned_incident(
            two_orgs.incident_a.id, two_orgs.user_a, two_orgs.db
        )
        expected = two_orgs.incident_a
    else:
        call = ownership_module.get_owned_slo(
            two_orgs.slo_a.id,
            two_orgs.cluster_a.id,
            two_orgs.user_a,
            two_orgs.db,
        )
        expected = two_orgs.slo_a
    assert asyncio.run(call) is expected


@pytest.mark.parametrize("resource", ["cluster", "incident", "slo"])
@pytest.mark.parametrize("caller", ["other_org", "missing"])
def test_cross_org_and_missing_ids_are_indistinguishable_404s(
    ownership_module, two_orgs, resource, caller
):
    resource_id = (
        uuid.uuid4()
        if caller == "missing"
        else getattr(two_orgs, f"{resource}_a").id
    )
    user = two_orgs.user_b if caller == "other_org" else two_orgs.user_a
    if resource == "cluster":
        call = ownership_module.get_owned_cluster(resource_id, user, two_orgs.db)
    elif resource == "incident":
        call = ownership_module.get_owned_incident(resource_id, user, two_orgs.db)
    else:
        call = ownership_module.get_owned_slo(
            resource_id, two_orgs.cluster_a.id, user, two_orgs.db
        )

    with pytest.raises(ownership_module._test_http_exception) as exc_info:
        asyncio.run(call)
    assert exc_info.value.status_code == 404


def test_slo_id_cannot_be_substituted_across_clusters_in_same_org(
    ownership_module, two_orgs
):
    other_cluster = SimpleNamespace(id=uuid.uuid4(), org_id=two_orgs.user_a.org_id)
    two_orgs.db.rows[ownership_module._test_models.Cluster].append(other_cluster)
    two_orgs.db.clusters[other_cluster.id] = other_cluster

    call = ownership_module.get_owned_slo(
        two_orgs.slo_a.id, other_cluster.id, two_orgs.user_a, two_orgs.db
    )
    with pytest.raises(ownership_module._test_http_exception) as exc_info:
        asyncio.run(call)
    assert exc_info.value.status_code == 404


def _function_source(relative_path: str, function_name: str) -> str:
    path = _ROOT / relative_path
    source = path.read_text()
    tree = ast.parse(source)
    node = next(
        item
        for item in ast.walk(tree)
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name == function_name
    )
    lines = source.splitlines()
    return "\n".join(lines[node.lineno - 1 : node.end_lineno])


@pytest.mark.parametrize(
    ("relative_path", "function_name", "dependency"),
    [
        ("sre_agent/api/v1/clusters.py", "update_cluster_endpoint", "get_owned_cluster"),
        ("sre_agent/api/v1/clusters.py", "get_cluster_health", "get_owned_cluster"),
        ("sre_agent/api/v1/clusters.py", "delete_cluster", "get_owned_cluster"),
        ("sre_agent/api/v1/clusters.py", "set_cluster_lock", "get_owned_cluster"),
        ("sre_agent/api/v1/clusters.py", "get_cluster_audit_logs", "get_owned_cluster"),
        ("sre_agent/api/v1/mission_control.py", "get_incident_transcript", "get_owned_incident"),
        ("sre_agent/api/v1/mission_control.py", "get_incident_audit_logs", "get_owned_incident"),
        ("sre_agent/api/v1/mission_control.py", "send_incident_message", "get_owned_incident"),
        ("sre_agent/api/v1/mission_control.py", "get_incident_status", "get_owned_incident"),
        ("sre_agent/api/v1/mission_control.py", "get_incident_agent_metrics", "get_owned_incident"),
        ("sre_agent/api/v1/mission_control.py", "approve_incident_action", "get_owned_incident"),
        ("sre_agent/api/v1/slos.py", "create_slo", "get_owned_cluster"),
        ("sre_agent/api/v1/slos.py", "list_slos", "get_owned_cluster"),
        ("sre_agent/api/v1/slos.py", "get_slo_status", "get_owned_slo"),
        ("sre_agent/api/v1/slos.py", "delete_slo_endpoint", "get_owned_slo"),
    ],
)
def test_bare_id_routes_use_central_ownership_dependency(
    relative_path, function_name, dependency
):
    block = _function_source(relative_path, function_name)
    assert f"Depends({dependency})" in block
