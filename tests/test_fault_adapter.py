#!/usr/bin/env python3
"""Tests for the executable Meridian scenario fault adapter."""

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS = ROOT / "evals" / "benchmarks"
_MODULE_PATH = BENCHMARKS / "fault_adapter.py"
_spec = importlib.util.spec_from_file_location("fault_adapter", _MODULE_PATH)
fault_adapter = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = fault_adapter
_spec.loader.exec_module(fault_adapter)

CHECKOUT = "http://localhost:8001"
PAYMENT = "http://localhost:8004"
SERVICE_URLS = {"checkout-service": CHECKOUT, "payment-service": PAYMENT}


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
    """A config service per URL prefix; POSTs merge unless told to misbehave."""

    def __init__(self, configs, *, refuse=None, fail_restore=()):
        self.configs = {host: dict(state) for host, state in configs.items()}
        self.refuse = dict(refuse or {})
        self.fail_restore = set(fail_restore)
        self.calls = []

    def _host(self, url):
        return url.rsplit("/admin", 1)[0]

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return FakeResponse(dict(self.configs[self._host(url)]))

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        host = self._host(url)
        payload = kwargs["json"]
        restoring = all(
            self.configs[host].get(key) != value for key, value in payload.items()
        ) and any(value in (0.0, False) for value in payload.values())
        if restoring and host in self.fail_restore:
            raise RuntimeError("service refused the restore")
        if host in self.refuse:
            # Confirm something other than what was asked for.
            return FakeResponse({**self.configs[host], **self.refuse[host]})
        self.configs[host].update(payload)
        return FakeResponse(dict(self.configs[host]))


def _contract(target, inject, cleanup, path="/admin/config"):
    return {
        "target": target,
        "path": path,
        "inject": dict(inject),
        "cleanup": dict(cleanup),
    }


def _scenario(*contracts, adapter="meridian_admin_config_v1", name="checkout_error"):
    declared = list(contracts) or [
        _contract("checkout-service", {"error_rate": 0.5}, {"error_rate": 0.0})
    ]
    return SimpleNamespace(
        name=name, fault={"adapter": adapter, "contracts": declared}
    )


def test_inject_and_cleanup_verify_config_and_restore_snapshot():
    client = FakeClient({CHECKOUT: {"error_rate": 0.0, "slow_rate": 0.0}})
    adapter = fault_adapter.MeridianAdminConfigAdapter(SERVICE_URLS)

    leases = asyncio.run(adapter.inject(client, _scenario()))
    asyncio.run(adapter.cleanup(client, leases))

    assert len(leases) == 1
    assert leases[0].original_values == {"error_rate": 0.0}
    assert client.calls[1][0:2] == ("POST", f"{CHECKOUT}/admin/config")
    assert client.calls[1][2]["json"] == {"error_rate": 0.5}
    assert client.calls[2][2]["json"] == {"error_rate": 0.0}
    assert client.configs[CHECKOUT] == {"error_rate": 0.0, "slow_rate": 0.0}


def test_multiple_contracts_apply_in_order_and_unwind_in_reverse():
    client = FakeClient(
        {
            CHECKOUT: {"error_rate": 0.0, "slow_rate": 0.0},
            PAYMENT: {"error_rate": 0.0, "slow_rate": 0.0},
        }
    )
    adapter = fault_adapter.MeridianAdminConfigAdapter(SERVICE_URLS)

    leases = asyncio.run(
        adapter.inject(
            client,
            _scenario(
                _contract("checkout-service", {"error_rate": 0.5}, {"error_rate": 0.0}),
                _contract("payment-service", {"slow_rate": 0.02}, {"slow_rate": 0.0}),
            ),
        )
    )
    assert [lease.target for lease in leases] == [
        "checkout-service",
        "payment-service",
    ]
    assert client.configs[CHECKOUT]["error_rate"] == 0.5
    assert client.configs[PAYMENT]["slow_rate"] == 0.02

    asyncio.run(adapter.cleanup(client, leases))
    posts = [call for call in client.calls if call[0] == "POST"]
    assert [call[1] for call in posts[-2:]] == [
        f"{PAYMENT}/admin/config",
        f"{CHECKOUT}/admin/config",
    ]
    assert client.configs[CHECKOUT]["error_rate"] == 0.0
    assert client.configs[PAYMENT]["slow_rate"] == 0.0


def test_a_failing_later_contract_unwinds_the_contracts_already_applied():
    client = FakeClient(
        {
            CHECKOUT: {"error_rate": 0.0},
            PAYMENT: {"slow_rate": 0.0},
        },
        refuse={PAYMENT: {"slow_rate": 0.9}},
    )
    adapter = fault_adapter.MeridianAdminConfigAdapter(SERVICE_URLS)

    with pytest.raises(fault_adapter.FaultAdapterError, match="did not confirm"):
        asyncio.run(
            adapter.inject(
                client,
                _scenario(
                    _contract(
                        "checkout-service", {"error_rate": 0.5}, {"error_rate": 0.0}
                    ),
                    _contract(
                        "payment-service", {"slow_rate": 0.02}, {"slow_rate": 0.0}
                    ),
                ),
            )
        )

    # The scenario never ran, so checkout must not stay degraded for the next one.
    assert client.configs[CHECKOUT] == {"error_rate": 0.0}


def test_a_failed_unwind_is_reported_and_not_swallowed():
    client = FakeClient(
        {
            CHECKOUT: {"error_rate": 0.0},
            PAYMENT: {"slow_rate": 0.0},
        },
        refuse={PAYMENT: {"slow_rate": 0.9}},
        fail_restore={CHECKOUT},
    )
    adapter = fault_adapter.MeridianAdminConfigAdapter(SERVICE_URLS)

    with pytest.raises(fault_adapter.FaultAdapterError, match="unwinding"):
        asyncio.run(
            adapter.inject(
                client,
                _scenario(
                    _contract(
                        "checkout-service", {"error_rate": 0.5}, {"error_rate": 0.0}
                    ),
                    _contract(
                        "payment-service", {"slow_rate": 0.02}, {"slow_rate": 0.0}
                    ),
                ),
            )
        )


def test_cleanup_tries_every_lease_before_raising():
    client = FakeClient(
        {
            CHECKOUT: {"error_rate": 0.0},
            PAYMENT: {"slow_rate": 0.0},
        },
        fail_restore={PAYMENT},
    )
    adapter = fault_adapter.MeridianAdminConfigAdapter(SERVICE_URLS)
    leases = asyncio.run(
        adapter.inject(
            client,
            _scenario(
                _contract("checkout-service", {"error_rate": 0.5}, {"error_rate": 0.0}),
                _contract("payment-service", {"slow_rate": 0.02}, {"slow_rate": 0.0}),
            ),
        )
    )

    with pytest.raises(fault_adapter.FaultAdapterError, match="payment-service"):
        asyncio.run(adapter.cleanup(client, leases))
    # Payment failed first (reverse order) but checkout was still restored.
    assert client.configs[CHECKOUT] == {"error_rate": 0.0}


def test_inject_rejects_baseline_that_does_not_match_cleanup_contract():
    client = FakeClient({CHECKOUT: {"error_rate": 0.2}})
    adapter = fault_adapter.MeridianAdminConfigAdapter(SERVICE_URLS)

    with pytest.raises(fault_adapter.FaultAdapterError, match="baseline"):
        asyncio.run(adapter.inject(client, _scenario()))
    assert [call[0] for call in client.calls] == ["GET"]


def test_inject_fails_when_service_does_not_confirm_requested_config():
    client = FakeClient(
        {CHECKOUT: {"error_rate": 0.0}}, refuse={CHECKOUT: {"error_rate": 0.1}}
    )
    adapter = fault_adapter.MeridianAdminConfigAdapter(SERVICE_URLS)

    with pytest.raises(fault_adapter.FaultAdapterError, match="did not confirm"):
        asyncio.run(adapter.inject(client, _scenario()))
    assert client.calls[-1][2]["json"] == {"error_rate": 0.0}


def test_adapter_rejects_unknown_target_and_unsupported_contract():
    adapter = fault_adapter.MeridianAdminConfigAdapter(SERVICE_URLS)
    client = FakeClient({CHECKOUT: {}})

    with pytest.raises(fault_adapter.FaultAdapterError, match="target"):
        asyncio.run(
            adapter.inject(
                client,
                _scenario(
                    _contract("unknown-service", {"error_rate": 0.5}, {"error_rate": 0.0})
                ),
            )
        )
    with pytest.raises(fault_adapter.FaultAdapterError, match="adapter"):
        asyncio.run(adapter.inject(client, _scenario(adapter="shell_v1")))
    with pytest.raises(fault_adapter.FaultAdapterError, match="contracts"):
        asyncio.run(
            adapter.inject(
                client,
                SimpleNamespace(
                    name="empty",
                    fault={"adapter": "meridian_admin_config_v1", "contracts": []},
                ),
            )
        )


def test_adapter_rejects_duplicate_targets_and_absolute_paths():
    adapter = fault_adapter.MeridianAdminConfigAdapter(SERVICE_URLS)
    client = FakeClient({CHECKOUT: {"error_rate": 0.0, "slow_rate": 0.0}})

    with pytest.raises(fault_adapter.FaultAdapterError, match="duplicate"):
        asyncio.run(
            adapter.inject(
                client,
                _scenario(
                    _contract(
                        "checkout-service", {"error_rate": 0.5}, {"error_rate": 0.0}
                    ),
                    _contract(
                        "checkout-service", {"slow_rate": 0.5}, {"slow_rate": 0.0}
                    ),
                ),
            )
        )
    with pytest.raises(fault_adapter.FaultAdapterError, match="relative"):
        asyncio.run(
            adapter.inject(
                client,
                _scenario(
                    _contract(
                        "checkout-service",
                        {"error_rate": 0.5},
                        {"error_rate": 0.0},
                        path="//evil.example.com/admin/config",
                    )
                ),
            )
        )


def test_every_v2_scenario_injects_and_restores_against_the_declared_fixtures():
    """The corpus is only real if the adapter can actually drive all of it.

    The fake services are built from `fixtures.json` baselines, so this fails
    if a scenario's cleanup payload drifts from the manifest, if a target is
    unreachable from the bench's service map, or if a contract cannot round
    trip — the three ways a scenario passes load-time validation and still
    cannot run.
    """
    import importlib.util as _il
    import json

    sys.path.insert(0, str(BENCHMARKS))
    _ds_spec = _il.spec_from_file_location(
        "scenario_dataset", BENCHMARKS / "scenario_dataset.py"
    )
    scenario_dataset = _il.module_from_spec(_ds_spec)
    sys.modules[_ds_spec.name] = scenario_dataset
    _ds_spec.loader.exec_module(scenario_dataset)

    datasets = BENCHMARKS / "datasets"
    manifest = json.loads((datasets / "v2" / "fixtures.json").read_text())
    urls = {
        target: f"http://localhost:900{index}"
        for index, target in enumerate(sorted(manifest["targets"]))
    }
    baselines = {
        urls[target]: {
            knob: declared["baseline"]
            for knob, declared in spec["knobs"].items()
        }
        for target, spec in manifest["targets"].items()
    }
    adapter = fault_adapter.MeridianAdminConfigAdapter(urls)

    checked = 0
    for split in ("train", "dev", "holdout"):
        dataset = scenario_dataset.load_dataset(
            datasets, "v2", split, allow_holdout=split == "holdout", ci=False
        )
        for scenario in dataset.scenarios:
            client = FakeClient(baselines)
            leases = asyncio.run(adapter.inject(client, scenario))
            assert len(leases) == len(scenario.fault["contracts"])
            for lease, contract in zip(leases, scenario.fault["contracts"]):
                host = urls[contract["target"]]
                for knob, value in contract["inject"].items():
                    assert client.configs[host][knob] == value
                assert lease.original_values == contract["cleanup"]
            asyncio.run(adapter.cleanup(client, leases))
            assert client.configs == baselines, scenario.name
            checked += 1
    assert checked >= 20


def test_adapter_rejects_credentialed_or_non_http_service_urls():
    with pytest.raises(fault_adapter.FaultAdapterError, match="URL"):
        fault_adapter.MeridianAdminConfigAdapter(
            {"checkout-service": "file:///tmp/service"}
        )
    with pytest.raises(fault_adapter.FaultAdapterError, match="credentials"):
        fault_adapter.MeridianAdminConfigAdapter(
            {"checkout-service": "http://user:pass@localhost:8001"}
        )
