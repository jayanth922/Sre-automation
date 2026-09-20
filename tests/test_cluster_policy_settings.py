"""Per-cluster operational policy: the environment label and the approval window.

Both moved onto the cluster row because a single process-wide env var cannot
describe a platform where every Cluster is a different tenant's namespace --
one SENTINEL_CLUSTER_ENVIRONMENT was labelling a staging cluster and a
production cluster identically.

These are unit tests on purpose. The whole point of the change is that
resolution stays a pure function of (column, env var, default), so it can be
pinned exactly without a database standing in the way.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend import schemas
from sre_agent.approval_flow import approval_ttl
from sre_agent.execution_context import cluster_environment


@pytest.fixture(autouse=True)
def _no_inherited_operator_defaults(monkeypatch):
    """Neither knob may read a value this process happened to inherit.

    Without this the fallback tests below would pass or fail depending on the
    shell that launched pytest, which is exactly the coupling the change is
    meant to remove.
    """
    monkeypatch.delenv("SENTINEL_CLUSTER_ENVIRONMENT", raising=False)
    monkeypatch.delenv("APPROVAL_TTL_MINUTES", raising=False)
    yield


def _cluster(**overrides):
    row = {"environment": None, "approval_ttl_minutes": None}
    row.update(overrides)
    return SimpleNamespace(**row)


# --------------------------------------------------------------- environment


def test_the_cluster_column_beats_the_operator_default(monkeypatch):
    monkeypatch.setenv("SENTINEL_CLUSTER_ENVIRONMENT", "production")
    assert cluster_environment(_cluster(environment="staging")) == "staging"


def test_a_cluster_without_an_opinion_follows_the_operator(monkeypatch):
    monkeypatch.setenv("SENTINEL_CLUSTER_ENVIRONMENT", "development")
    assert cluster_environment(_cluster()) == "development"


def test_with_nothing_set_anywhere_a_cluster_is_production():
    """Fail closed: an unlabelled cluster gets production's treatment."""
    assert cluster_environment(_cluster()) == "production"


def test_an_unrecognised_label_cannot_downgrade_a_cluster():
    """A typo must not be a way to make a production cluster look like staging."""
    assert cluster_environment(_cluster(environment="stagging")) == "production"


def test_a_row_predating_the_column_still_resolves():
    """from_cluster is handed whatever the ORM produced.

    A cluster object without the attribute at all -- a stale object, a test
    double, a row read before the migration -- must resolve rather than raise.
    """
    assert cluster_environment(SimpleNamespace(name="legacy")) == "production"


def test_the_schema_rejects_a_misspelled_environment():
    """This is the half that makes the fail-closed fallback tolerable.

    The fallback silently normalises "stagging" to production; the Literal is
    what turns that same typo into a 422 the admin can actually see, instead of
    a saved setting that never did anything.
    """
    with pytest.raises(ValidationError):
        schemas.ClusterUpdate(environment="stagging")
    assert schemas.ClusterUpdate(environment="staging").environment == "staging"


# ------------------------------------------------------------- approval TTL


def test_the_cluster_ttl_beats_the_operator_default(monkeypatch):
    monkeypatch.setenv("APPROVAL_TTL_MINUTES", "30")
    assert approval_ttl(_cluster(approval_ttl_minutes=5)) == timedelta(minutes=5)


def test_a_cluster_without_a_ttl_follows_the_operator(monkeypatch):
    monkeypatch.setenv("APPROVAL_TTL_MINUTES", "45")
    assert approval_ttl(_cluster()) == timedelta(minutes=45)


def test_no_cluster_at_all_is_the_old_env_path_unchanged():
    """approval_ttl() keeps working for callers that have no cluster in scope."""
    assert approval_ttl() == timedelta(minutes=30)
    assert approval_ttl(None) == timedelta(minutes=30)


def test_a_ttl_is_clamped_to_something_a_human_can_act_on():
    """Zero would expire every approval at the instant it was created."""
    assert approval_ttl(_cluster(approval_ttl_minutes=0)) == timedelta(minutes=1)
    assert approval_ttl(_cluster(approval_ttl_minutes=-10)) == timedelta(minutes=1)


def test_a_nonsense_ttl_falls_back_rather_than_raising(monkeypatch):
    """A bad env var must not take the approval path down with it."""
    monkeypatch.setenv("APPROVAL_TTL_MINUTES", "soon")
    assert approval_ttl(_cluster()) == timedelta(minutes=30)


def test_the_schema_rejects_a_ttl_outside_the_usable_range():
    with pytest.raises(ValidationError):
        schemas.ClusterUpdate(approval_ttl_minutes=0)
    with pytest.raises(ValidationError):
        schemas.ClusterUpdate(approval_ttl_minutes=100_000)
    assert schemas.ClusterUpdate(approval_ttl_minutes=15).approval_ttl_minutes == 15


def test_both_settings_are_clearable_back_to_the_default():
    """Sending null is how an admin stops overriding.

    crud.update_cluster puts these in its clearable loop, so the API has to
    accept an explicit null rather than treating it as "field omitted".
    """
    update = schemas.ClusterUpdate(environment=None, approval_ttl_minutes=None)
    dumped = update.model_dump(exclude_unset=True)
    assert dumped == {"environment": None, "approval_ttl_minutes": None}
