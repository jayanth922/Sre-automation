#!/usr/bin/env python3
"""Route-level auth coverage tests (PR-T02).

Source-inspection only, deliberately not importing ``sre_agent.agent_runtime``
or constructing the real FastAPI ``app``: that module transitively pulls in
fastapi/langgraph/langchain-core/sqlalchemy/backend.database, none of which are
available in this lightweight test environment — the same constraint that
`tests/test_checkpointer.py::test_agent_runtime_uses_configured_checkpointer`
already works around by asserting on source text instead of on live objects.

Each authenticated router in ``sre_agent/api/v1/`` must attach
``get_current_user_and_org`` at the *router* level (``APIRouter(...,
dependencies=[Depends(get_current_user_and_org)])``) rather than relying on
per-route annotations, so a route added later without an explicit
``Depends(...)`` still can't be reached unauthenticated. Two routers are
legitimately exempt because they authenticate a different caller:

- ``alerts.py``: its one route is called by the client's own Alertmanager via
  a cluster token, not a logged-in user.
- ``backend/routers/auth.py``: login/register/token/refresh must stay public;
  only its ``/me`` and ``/password`` sub-routes carry per-route user auth.

The legacy single-tenant endpoints defined directly on ``app`` in
``agent_runtime.py`` (``/invocations``, ``/agent/metrics``, ``/agent/state``,
``/agent/state/{session_id}``, ``/approve/{session_id}``) predate the
multi-tenant org/user model, so they can't authenticate via
``get_current_user_and_org`` either — they're gated by
``require_internal_token`` instead. ``/health``-style endpoints (``/ping``,
``/platform-metrics``) are intentionally public and excluded here.
"""

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_API_V1 = _ROOT / "sre_agent" / "api" / "v1"

# Every router mounted under /api/v1 that serves logged-in dashboard/API users.
USER_AUTH_ROUTERS = [
    "analytics.py",
    "chat.py",
    "clusters.py",
    "incidents.py",
    "jobs.py",
    "members.py",
    "metrics.py",
    "mission_control.py",
    "recommendations.py",
    "runbooks.py",
    "services.py",
    "slos.py",
    "ws_tickets.py",
]

# Legacy globals defined directly on `app` in agent_runtime.py — gated by a
# shared internal token instead of a user JWT (see docstring above).
INTERNAL_ONLY_ROUTE_DECORATORS = [
    '@app.get("/agent/metrics", dependencies=[Depends(require_internal_token)])',
    '@app.post(\n    "/invocations",\n    response_model=InvocationResponse,\n    dependencies=[Depends(require_internal_token)],\n)',
    '@app.get("/agent/state", dependencies=[Depends(require_internal_token)])',
    '@app.get("/agent/state/{session_id}", dependencies=[Depends(require_internal_token)])',
    '@app.post("/approve/{session_id}", dependencies=[Depends(require_internal_token)])',
    '@app.post(\n    "/webhook/alert",\n    status_code=202,\n    dependencies=[Depends(require_internal_token)],\n)',
]

# Endpoints that are intentionally public and must NOT be auth-gated.
PUBLIC_ROUTE_DECORATORS = [
    '@app.api_route("/ping", methods=["GET", "HEAD"])',
]


def _matching_close_paren(src: str, open_paren_idx: int) -> int:
    """Index of the ``)`` that matches the ``(`` at ``open_paren_idx``."""
    depth = 0
    for i in range(open_paren_idx, len(src)):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    raise ValueError("unbalanced parentheses")


def _router_block(rel_filename: str) -> str:
    """Return the source text of the ``router = APIRouter(...)`` call."""
    src = (_API_V1 / rel_filename).read_text()
    start = src.index("router = APIRouter(")
    open_paren = start + len("router = APIRouter")
    end = _matching_close_paren(src, open_paren)
    return src[start : end + 1]


@pytest.mark.parametrize("filename", USER_AUTH_ROUTERS)
def test_router_requires_user_auth_at_router_level(filename):
    block = _router_block(filename)
    assert "dependencies=[Depends(get_current_user_and_org)]" in block, (
        f"{filename}: APIRouter(...) must attach get_current_user_and_org at "
        "the router level, not rely on per-route Depends(...) annotations"
    )


def test_alerts_router_uses_cluster_token_not_user_auth():
    """Alertmanager calls this with a cluster token, not a user JWT — router-
    level get_current_user_and_org would break the webhook, not fix a gap."""
    src = (_API_V1 / "alerts.py").read_text()
    assert "get_current_user_and_org" not in src
    assert "Depends(_get_cluster_from_token)" in src


def test_auth_router_login_endpoints_stay_public():
    src = (_ROOT / "backend" / "routers" / "auth.py").read_text()
    block_start = src.index("router = APIRouter(")
    block_end = src.index(")", block_start)
    assert "get_current_user_and_org" not in src[block_start : block_end + 1]
    for public_path in ('"/register"', '"/token"', '"/refresh"', '"/logout"'):
        assert public_path in src


def test_auth_router_me_and_password_require_user_auth():
    src = (_ROOT / "backend" / "routers" / "auth.py").read_text()
    for route_decorator, fn_name in (
        ('@router.get("/me"', "read_current_user"),
        ('@router.post("/password")', "reset_password"),
    ):
        idx = src.index(route_decorator)
        fn_idx = src.index(f"def {fn_name}", idx)
        body_end = src.index("):", fn_idx)
        assert "Depends(get_current_user_and_org)" in src[idx:body_end]


@pytest.mark.parametrize("decorator", INTERNAL_ONLY_ROUTE_DECORATORS)
def test_legacy_global_endpoint_requires_internal_token(decorator):
    src = (_ROOT / "sre_agent" / "agent_runtime.py").read_text()
    assert decorator in src


@pytest.mark.parametrize("decorator", PUBLIC_ROUTE_DECORATORS)
def test_health_endpoint_stays_public(decorator):
    src = (_ROOT / "sre_agent" / "agent_runtime.py").read_text()
    assert decorator in src
    assert "dependencies=[Depends(require_internal_token)]" not in src.split(decorator, 1)[1].split("\n\n", 1)[0]


def test_divergent_rbac_module_removed():
    assert not (_ROOT / "backend" / "rbac.py").exists()


def test_no_remaining_backend_rbac_imports():
    import subprocess

    result = subprocess.run(
        ["grep", "-rl", "backend.rbac", "--include=*.py", str(_ROOT / "sre_agent"), str(_ROOT / "backend")],
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == ""


def test_auth_deps_require_admin_is_canonical():
    src = (_ROOT / "sre_agent" / "api" / "v1" / "auth_deps.py").read_text()
    assert "async def require_admin(" in src
    assert "Depends(get_current_user_and_org)" in src


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
