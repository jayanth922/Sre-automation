#!/usr/bin/env python3
"""Tests for the executable Meridian scenario fault adapter."""

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS = ROOT / "benchmarks"
_MODULE_PATH = BENCHMARKS / "fault_adapter.py"
_spec = importlib.util.spec_from_file_location("fault_adapter", _MODULE_PATH)
fault_adapter = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = fault_adapter
_spec.loader.exec_module(fault_adapter)


class FakeResponse:
    def __init__(self, payload, *, status_error=None):
        self._payload = payload
        self._status_error = status_error

    def raise_for_status(self):
        if self._status_error:
            raise self._status_error

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, get_payload, post_payloads):
        self.get_payload = get_payload
        self.post_payloads = list(post_payloads)
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return FakeResponse(self.get_payload)

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return FakeResponse(self.post_payloads.pop(0))


def _scenario(**fault_overrides):
    fault = {
        "adapter": "meridian_admin_config_v1",
        "target": "checkout-service",
        "inject": {
            "path": "/admin/config",
            "payload": {"error_rate": 0.5},
        },
        "cleanup": {
            "path": "/admin/config",
            "payload": {"error_rate": 0.0},
        },
    }
    fault.update(fault_overrides)
    return SimpleNamespace(name="checkout_error", fault=fault)


def test_inject_and_cleanup_verify_config_and_restore_snapshot():
    client = FakeClient(
        {"error_rate": 0.0, "slow_rate": 0.0},
        [
            {"error_rate": 0.5, "slow_rate": 0.0},
            {"error_rate": 0.0, "slow_rate": 0.0},
        ],
    )
    adapter = fault_adapter.MeridianAdminConfigAdapter(
        {"checkout-service": "http://localhost:8001"}
    )

    lease = asyncio.run(adapter.inject(client, _scenario()))
    asyncio.run(adapter.cleanup(client, lease))

    assert lease.original_values == {"error_rate": 0.0}
    assert client.calls[1][0:2] == (
        "POST",
        "http://localhost:8001/admin/config",
    )
    assert client.calls[1][2]["json"] == {"error_rate": 0.5}
    assert client.calls[2][2]["json"] == {"error_rate": 0.0}


def test_inject_rejects_baseline_that_does_not_match_cleanup_contract():
    client = FakeClient({"error_rate": 0.2}, [])
    adapter = fault_adapter.MeridianAdminConfigAdapter(
        {"checkout-service": "http://localhost:8001"}
    )

    with pytest.raises(fault_adapter.FaultAdapterError, match="baseline"):
        asyncio.run(adapter.inject(client, _scenario()))
    assert [call[0] for call in client.calls] == ["GET"]


def test_inject_fails_when_service_does_not_confirm_requested_config():
    client = FakeClient(
        {"error_rate": 0.0},
        [{"error_rate": 0.1}, {"error_rate": 0.0}],
    )
    adapter = fault_adapter.MeridianAdminConfigAdapter(
        {"checkout-service": "http://localhost:8001"}
    )

    with pytest.raises(fault_adapter.FaultAdapterError, match="did not confirm"):
        asyncio.run(adapter.inject(client, _scenario()))
    assert client.calls[-1][2]["json"] == {"error_rate": 0.0}


def test_adapter_rejects_unknown_target_and_unsupported_contract():
    adapter = fault_adapter.MeridianAdminConfigAdapter(
        {"checkout-service": "http://localhost:8001"}
    )
    client = FakeClient({}, [])

    with pytest.raises(fault_adapter.FaultAdapterError, match="target"):
        asyncio.run(adapter.inject(client, _scenario(target="unknown-service")))
    with pytest.raises(fault_adapter.FaultAdapterError, match="adapter"):
        asyncio.run(adapter.inject(client, _scenario(adapter="shell_v1")))


def test_adapter_rejects_credentialed_or_non_http_service_urls():
    with pytest.raises(fault_adapter.FaultAdapterError, match="URL"):
        fault_adapter.MeridianAdminConfigAdapter(
            {"checkout-service": "file:///tmp/service"}
        )
    with pytest.raises(fault_adapter.FaultAdapterError, match="credentials"):
        fault_adapter.MeridianAdminConfigAdapter(
            {"checkout-service": "http://user:pass@localhost:8001"}
        )
