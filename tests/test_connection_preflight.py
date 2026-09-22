#!/usr/bin/env python3
"""The preflight must grade the credential the agent will actually use.

Settings stores an encrypted per-cluster GitHub PAT, and
`ExecutionContext.from_cluster` reads it. While this check consulted only
`GITHUB_TOKEN`, the Settings page -- whose whole job is telling a new operator
whether their setup is wired -- answered about a different credential: red for
a tenant who configured one correctly, green for a tenant who configured
nothing on a host that happens to export the variable.
"""

from sre_agent.api.v1.services import _github_token


class _Cluster:
    def __init__(self, token):
        self.github_token = token


def test_the_cluster_pat_wins_over_the_process_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "from-the-host")
    assert _github_token(_Cluster("from-settings")) == "from-settings"


def test_the_env_var_is_only_a_fallback(monkeypatch):
    """Single-tenant local runs still work -- `from_environment` is their context."""
    monkeypatch.setenv("GITHUB_TOKEN", "from-the-host")
    assert _github_token(_Cluster(None)) == "from-the-host"


def test_nothing_configured_anywhere_is_no_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert _github_token(_Cluster(None)) is None


def test_a_row_without_the_attribute_does_not_raise(monkeypatch):
    """The check runs against whatever `_load_cluster` returns; it must not 500."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    class _Bare:
        pass

    assert _github_token(_Bare()) is None
