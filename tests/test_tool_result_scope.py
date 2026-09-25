"""A tool result must say which question it answered.

Two tools were answering narrower questions than the agent believed, with
nothing in the payload to say so:

- An instant Prometheus query describes one moment. Once the number reaches the
  specialist's transcript that moment is invisible, so a specialist
  reconstructing a window that has already passed, who omits `time`, is handed
  the value NOW and reads it as the incident's value.
- `get_deployment_spec` reads the declared pod template. In trial 5 the agent
  read SLOW_QUERY_RATE="0" and FAULT_INJECTION_ENABLED="false" there and
  EXCLUDED the injected-slow-query branch - while the live rate was 1.0, set
  through the service's own admin API, which leaves the pod template identical.
  Those are startup defaults the runtime had already superseded.

These tests pin both results to stating their own scope.
"""

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_MCP = Path(__file__).resolve().parents[1] / "services" / "edge_mcp_servers" / "mcp_servers"


def _stub(name: str, **attrs):
    """Each MCP server's API client ships in that server's own container image,
    not in this venv. Stub what the module body imports - every path these tests
    exercise is either pure or has its client patched out.
    """
    if name in sys.modules:
        return sys.modules[name]
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[name] = module
        return module


def _api_stub_module(name: str):
    """A stub package whose every attribute is a fresh class.

    k8s_real annotates with `client.CoreV1Api` at import time, and `Optional[x]`
    rejects anything that is not a type - so these have to be classes, not mocks.
    Dunders are left to fail normally: the import machinery reads `__path__` and
    would otherwise try to iterate a class.
    """
    module = types.ModuleType(name)
    module.__path__ = []
    made = {}

    def _getattr(attr):
        if attr.startswith("__") and attr.endswith("__"):
            raise AttributeError(attr)
        return made.setdefault(attr, type(attr, (), {}))

    module.__getattr__ = _getattr
    return module


_stub("prometheus_api_client", PrometheusConnect=MagicMock())
if not importlib.util.find_spec("kubernetes"):
    _k8s = _api_stub_module("kubernetes")
    _k8s_client = _api_stub_module("kubernetes.client")
    _k8s_rest = types.ModuleType("kubernetes.client.rest")
    _k8s_rest.ApiException = type("ApiException", (Exception,), {})
    _k8s.client = _k8s_client
    _k8s.config = MagicMock()
    _k8s_client.rest = _k8s_rest
    sys.modules.update(
        {
            "kubernetes": _k8s,
            "kubernetes.client": _k8s_client,
            "kubernetes.client.rest": _k8s_rest,
        }
    )

# k8s_real/server.py imports its sibling `spec_view` by bare name.
sys.path.insert(0, str(_MCP / "k8s_real"))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prom = _load("prometheus_real_server", _MCP / "prometheus_real" / "server.py")
k8s = _load("k8s_real_server", _MCP / "k8s_real" / "server.py")


# An instant vector as Prometheus returns it: the sample carries the evaluation
# timestamp, and it is the only authoritative answer to "when is this from".
INCIDENT_UNIX = 1758594728.0  # 2025-09-23T02:32:08Z
SAMPLE = [
    {
        "metric": {"service": "inventory-service"},
        "value": [INCIDENT_UNIX, "1.84"],
    }
]


# --------------------------------------------------------------------------
# Prometheus: which instant does this number describe?
# --------------------------------------------------------------------------


def test_sample_timestamp_reads_prometheus_own_stamp():
    assert prom._sample_timestamp(SAMPLE) == INCIDENT_UNIX


@pytest.mark.parametrize(
    "result",
    [[], None, "Error: could not connect", [{"metric": {}}], [{"value": []}]],
)
def test_sample_timestamp_is_none_when_there_is_no_sample(result):
    assert prom._sample_timestamp(result) is None


@pytest.mark.parametrize(
    "value",
    ["2025-09-23T02:32:08Z", "2025-09-23T02:32:08+00:00", "1758594728", 1758594728, "5m"],
)
def test_coercion_reports_no_problem_for_forms_it_understands(value):
    resolved, reason = prom._coerce_with_reason(value)
    assert reason is None
    assert resolved.tzinfo is not None


def test_coercion_reports_when_it_silently_fell_back_to_now():
    """The fallback is right; the silence was not. It answered about now."""
    resolved, reason = prom._coerce_with_reason("during the deploy")
    assert reason and "could not parse" in reason
    assert resolved.tzinfo is not None


def test_omitting_time_is_flagged_as_not_evidence_about_a_past_window():
    stamp = prom._evaluation_stamp(SAMPLE, time_argument=None, requested=None)
    assert stamp["time_argument"] is None
    assert "NOW" in stamp["warning"]
    assert "get_metric_range" in stamp["warning"]


def test_a_stamped_result_reports_the_instant_prometheus_evaluated():
    requested, _ = prom._coerce_with_reason("2025-09-23T02:32:08Z")
    stamp = prom._evaluation_stamp(
        SAMPLE, time_argument="2025-09-23T02:32:08Z", requested=requested
    )
    assert "warning" not in stamp
    assert stamp["evaluated_at"].startswith("2025-09-23T02:32:08")
    assert stamp["evaluated_at_source"] == "prometheus sample"


def test_an_unparsable_time_is_reported_to_the_agent_not_only_the_log():
    stamp = prom._evaluation_stamp(
        SAMPLE,
        time_argument="during the deploy",
        requested=None,
        coerce_error="could not parse time='during the deploy'",
    )
    assert "TIME ARGUMENT IGNORED" in stamp["warning"]
    assert "NOT evidence about a past window" in stamp["warning"]


def test_an_empty_result_still_reports_the_instant_it_asked_about():
    requested, _ = prom._coerce_with_reason("2025-09-23T02:32:08Z")
    stamp = prom._evaluation_stamp([], time_argument="2025-09-23T02:32:08Z", requested=requested)
    assert stamp["evaluated_at"].startswith("2025-09-23T02:32:08")
    assert "no sample" in stamp["evaluated_at_source"]


def _fake_prom(result):
    client = MagicMock()
    client.custom_query.return_value = result
    return client


def test_get_metric_carries_the_stamp_alongside_the_value():
    with patch.object(prom, "get_prom_client", return_value=_fake_prom(SAMPLE)):
        payload = json.loads(asyncio.run(prom.get_metric(query="up", time="2025-09-23T02:32:08Z")))
    assert payload["result"] == SAMPLE
    assert payload["evaluated_at"].startswith("2025-09-23T02:32:08")
    assert payload["time_argument"] == "2025-09-23T02:32:08Z"
    assert "warning" not in payload


def test_get_metric_without_time_warns_in_the_payload_the_agent_reads():
    with patch.object(prom, "get_prom_client", return_value=_fake_prom(SAMPLE)):
        payload = json.loads(asyncio.run(prom.get_metric(query="up")))
    assert "NOW" in payload["warning"]


def test_golden_signals_carry_the_same_scope():
    with patch.object(prom, "get_prom_client", return_value=_fake_prom(SAMPLE)):
        payload = json.loads(
            asyncio.run(prom.get_golden_signals(service="inventory-service"))
        )
    scope = payload["query_scope"]
    assert "NOW" in scope["warning"]
    assert scope["evaluated_at_source"] == "prometheus sample"


def test_golden_signals_no_longer_swallow_an_unparsable_time():
    """It used to log the failure and answer about now without saying so."""
    with patch.object(prom, "get_prom_client", return_value=_fake_prom(SAMPLE)):
        payload = json.loads(
            asyncio.run(
                prom.get_golden_signals(service="inventory-service", time="during the deploy")
            )
        )
    assert "TIME ARGUMENT IGNORED" in payload["query_scope"]["warning"]


def test_get_metric_docstring_tells_the_agent_when_time_is_required():
    doc = prom.get_metric.__doc__
    assert "reconstructing an incident" in doc
    assert "evaluated_at" in doc


# --------------------------------------------------------------------------
# Kubernetes: declared spec is not running configuration
# --------------------------------------------------------------------------


def _fake_deployment():
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name="inventory-service",
            namespace="meridian",
            annotations={"deployment.kubernetes.io/revision": "4"},
        ),
        spec=SimpleNamespace(
            replicas=2,
            template=SimpleNamespace(
                spec=SimpleNamespace(containers=[], init_containers=[])
            ),
        ),
        status=SimpleNamespace(conditions=[]),
    )


def _deployment_spec():
    api = MagicMock()
    api.read_namespaced_deployment.return_value = _fake_deployment()
    with patch.object(k8s, "get_apps_v1_api", return_value=api):
        return json.loads(
            asyncio.run(
                k8s.handle_get_deployment_spec(
                    k8s.GetDeploymentSpecParams(
                        deployment_name="inventory-service", namespace="meridian"
                    )
                )
            )
        )


def test_deployment_spec_states_what_it_read():
    scope = _deployment_spec()["scope"]
    assert "DECLARED" in scope["shows"]
    assert "admin/config API" in scope["does_not_show"]


def test_deployment_spec_refuses_to_let_a_declared_value_close_a_branch():
    """Trial 5's real shape: inventory-service DECLARES SLOW_QUERY_RATE="0",
    SLOW_QUERY_DELAY_SECONDS="0" and FAULT_INJECTION_ENABLED="false", while the
    fault adapter drives the live rate to 1.0 through PUT /admin/config and
    leaves the pod template untouched. The agent read three specific, current-
    looking values that the runtime had superseded, and called the injection
    inert. A stale value is worse than a missing one, so the scope names it
    first."""
    scope = _deployment_spec()["scope"]
    claim = scope["not_evidence_of_runtime_state"]
    assert "STARTUP DEFAULT" in claim
    assert "can be live and on right now" in claim
    assert "absent is not a setting that is off" in claim


def test_deployment_spec_still_reports_the_declared_configuration():
    result = _deployment_spec()
    assert result["name"] == "inventory-service"
    assert result["replicas"] == 2
    assert result["revision"] == "4"


def test_get_deployment_spec_docstring_no_longer_promises_running_config():
    doc = k8s.get_deployment_spec.__doc__
    assert "actually configured to run" not in doc
    assert "NOT the running process's effective" in doc
    assert "STARTUP DEFAULT that may already have been superseded" in doc
