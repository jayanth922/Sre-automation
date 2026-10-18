#!/usr/bin/env python3
"""Safe executable adapter for Meridian `/admin/config` fault contracts.

A scenario declares one or more contracts, each naming a service, the config
keys to change, and the baseline those keys must already hold. The adapter
verifies the baseline before touching anything, applies the contracts in the
declared order, and unwinds everything it applied if any later contract fails.
The invariant that matters: a benchmark run never leaves a service in a state
the scenario did not declare, even when injection fails halfway through.

`/admin/config` faults live in process memory, so any restart clears them —
which makes restart the right answer to every one of them. A second adapter,
`k8s_deployment_env_v1`, injects through the Deployment instead: it rolls out
a new revision with changed environment variables, the way a bad config
deploy actually lands. That fault survives a restart and is undone only by
rolling back, so it is the one fault class here whose correct fix is a
rollback rather than whichever action replaces the process.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Optional, Sequence
from urllib.parse import urlparse

SUPPORTED_ADAPTER = "meridian_admin_config_v1"
DEPLOYMENT_ENV_ADAPTER = "k8s_deployment_env_v1"


class FaultAdapterError(RuntimeError):
    """A declared fault could not be applied or cleaned up safely."""


@dataclass(frozen=True)
class FaultLease:
    scenario: str
    target: str
    url: str
    path: str
    original_values: dict[str, Any]
    injected_values: dict[str, Any]
    injected_at: datetime


@dataclass(frozen=True)
class _Contract:
    target: str
    path: str
    inject: dict[str, Any]
    cleanup: dict[str, Any]


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FaultAdapterError(f"{field} must be an object")
    return value


def _response_object(response: Any, field: str) -> dict[str, Any]:
    response.raise_for_status()
    return _object(response.json(), field)


def _assert_values(
    actual: dict[str, Any], expected: dict[str, Any], field: str
) -> None:
    mismatches = {
        key: {"expected": value, "actual": actual.get(key)}
        for key, value in expected.items()
        if actual.get(key) != value
    }
    if mismatches:
        raise FaultAdapterError(
            f"{field} did not confirm requested values: {mismatches}"
        )


class MeridianAdminConfigAdapter:
    """Apply typed config faults and always restore the verified baseline."""

    adapter_id = SUPPORTED_ADAPTER
    lease_type = FaultLease

    def __init__(
        self, service_urls: dict[str, str], *, timeout_seconds: float = 6.0
    ) -> None:
        if not service_urls:
            raise FaultAdapterError("at least one service URL is required")
        normalized: dict[str, str] = {}
        for target, raw_url in service_urls.items():
            parsed = urlparse(raw_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise FaultAdapterError(f"service URL is invalid for {target}")
            if parsed.username or parsed.password:
                raise FaultAdapterError(
                    f"embedded service URL credentials are forbidden for {target}"
                )
            if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
                raise FaultAdapterError(
                    f"service URL must contain only scheme and authority for {target}"
                )
            normalized[target] = raw_url.rstrip("/")
        self._service_urls = normalized
        self._timeout_seconds = timeout_seconds

    def _contracts(self, scenario: Any) -> tuple[_Contract, ...]:
        fault = _object(getattr(scenario, "fault", None), "fault")
        if fault.get("adapter") != SUPPORTED_ADAPTER:
            raise FaultAdapterError(
                f"unsupported fault adapter: {fault.get('adapter')!r}"
            )
        declared = fault.get("contracts")
        if not isinstance(declared, (list, tuple)) or not declared:
            raise FaultAdapterError("fault.contracts must be a non-empty list")

        contracts: list[_Contract] = []
        seen: set[str] = set()
        for index, raw in enumerate(declared):
            contract = _object(raw, f"fault.contracts[{index}]")
            target = contract.get("target")
            if not isinstance(target, str) or target not in self._service_urls:
                raise FaultAdapterError(f"unsupported fault target: {target!r}")
            if target in seen:
                raise FaultAdapterError(f"duplicate fault target: {target}")
            seen.add(target)
            path = contract.get("path")
            if (
                not isinstance(path, str)
                or not path.startswith("/")
                or path.startswith("//")
            ):
                raise FaultAdapterError("fault contract path must be relative")
            inject = _object(contract.get("inject"), f"fault.contracts[{index}].inject")
            cleanup = _object(
                contract.get("cleanup"), f"fault.contracts[{index}].cleanup"
            )
            if not inject or set(inject) != set(cleanup):
                raise FaultAdapterError(
                    "inject and cleanup payloads must declare the same keys"
                )
            contracts.append(
                _Contract(
                    target=target,
                    path=path,
                    inject=dict(inject),
                    cleanup=dict(cleanup),
                )
            )
        return tuple(contracts)

    async def _restore(
        self,
        client: Any,
        *,
        url: str,
        original_values: dict[str, Any],
    ) -> None:
        response = await client.post(
            url,
            json=original_values,
            timeout=self._timeout_seconds,
        )
        restored = _response_object(response, "cleanup response")
        _assert_values(restored, original_values, "cleanup response")

    async def _inject_one(
        self, client: Any, scenario_name: str, contract: _Contract
    ) -> FaultLease:
        url = f"{self._service_urls[contract.target]}{contract.path}"
        current_response = await client.get(url, timeout=self._timeout_seconds)
        current = _response_object(current_response, "baseline response")
        original_values = {key: current.get(key) for key in contract.inject}
        _assert_values(original_values, contract.cleanup, "baseline")

        injected_at = datetime.now(timezone.utc)
        try:
            response = await client.post(
                url,
                json=contract.inject,
                timeout=self._timeout_seconds,
            )
            applied = _response_object(response, "injection response")
            _assert_values(applied, contract.inject, "injection response")
        except Exception as exc:
            try:
                await self._restore(client, url=url, original_values=original_values)
            except Exception as cleanup_exc:
                raise FaultAdapterError(
                    f"fault injection failed and cleanup also failed: {cleanup_exc}"
                ) from exc
            if isinstance(exc, FaultAdapterError):
                raise
            raise FaultAdapterError(f"fault injection failed: {exc}") from exc

        return FaultLease(
            scenario=scenario_name,
            target=contract.target,
            url=self._service_urls[contract.target],
            path=contract.path,
            original_values=original_values,
            injected_values=dict(contract.inject),
            injected_at=injected_at,
        )

    async def inject(self, client: Any, scenario: Any) -> tuple[FaultLease, ...]:
        """Apply every declared contract, unwinding all of them on failure."""
        contracts = self._contracts(scenario)
        scenario_name = str(getattr(scenario, "name", "unknown"))
        leases: list[FaultLease] = []
        for contract in contracts:
            try:
                leases.append(await self._inject_one(client, scenario_name, contract))
            except Exception as exc:
                # Contracts already applied are not the scenario's fault, but
                # leaving them applied would poison every run after this one.
                try:
                    await self.cleanup(client, leases)
                except Exception as unwind_exc:
                    raise FaultAdapterError(
                        f"fault injection failed on {contract.target} and "
                        f"unwinding earlier contracts also failed: {unwind_exc}"
                    ) from exc
                raise
        return tuple(leases)

    async def cleanup(
        self, client: Any, leases: Sequence[FaultLease] | Iterable[FaultLease]
    ) -> None:
        """Restore every lease, reporting the first failure only after trying all."""
        pending = list(leases)
        failures: list[str] = []
        # Reverse order so a scenario's contracts unwind the way they applied.
        for lease in reversed(pending):
            try:
                await self._restore(
                    client,
                    url=f"{lease.url}{lease.path}",
                    original_values=lease.original_values,
                )
            except Exception as exc:
                failures.append(f"{lease.target}: {exc}")
        if failures:
            raise FaultAdapterError(f"fault cleanup failed: {'; '.join(failures)}")


# ---------------------------------------------------- deployment env faults

_DEPLOYMENT_PATH = re.compile(
    r"^/apis/apps/v1/namespaces/(?P<namespace>[a-z0-9]([-a-z0-9]*[a-z0-9])?)"
    r"/deployments/(?P<name>[a-z0-9]([-a-z0-9]*[a-z0-9])?)$"
)
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")

# argv, timeout seconds -> (returncode, stdout, stderr)
CommandRunner = Callable[[Sequence[str], float], Awaitable[tuple[int, str, str]]]


async def _run_subprocess(argv: Sequence[str], timeout: float) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise FaultAdapterError(f"{argv[0]} timed out after {timeout:.0f}s")
    return proc.returncode or 0, stdout.decode(), stderr.decode()


def _env_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    return str(value)


def _env_matches(actual: Optional[str], expected: Any) -> bool:
    """Compare a pod-template env string to a declared knob value by type."""
    if actual is None:
        return False
    if isinstance(expected, bool):
        return actual.strip().lower() == _env_text(expected)
    if isinstance(expected, (int, float)):
        try:
            return float(actual) == float(expected)
        except ValueError:
            return False
    return actual == str(expected)


@dataclass(frozen=True)
class DeploymentEnvLease:
    scenario: str
    target: str
    namespace: str
    name: str
    container: str
    original_values: dict[str, str]
    injected_values: dict[str, Any]
    injected_at: datetime


@dataclass(frozen=True)
class _DeploymentContract:
    target: str
    namespace: str
    name: str
    inject: dict[str, Any]
    cleanup: dict[str, Any]


class KubernetesDeploymentEnvAdapter:
    """Roll out env-var faults as real Deployment revisions, then restore them.

    The target is `deployment/<name>` and the contract path is the
    Deployment's API path, so the namespace is declared, never defaulted.
    Every key must already be set to a literal value equal to the cleanup
    contract: an unset key would fall back to the application's own default,
    which is not a baseline anyone verified. Cleanup writes back the original
    strings, so a run that the agent already rolled back costs one no-op.
    """

    adapter_id = DEPLOYMENT_ENV_ADAPTER
    lease_type = DeploymentEnvLease

    def __init__(
        self,
        *,
        kubectl: str = "kubectl",
        allowed_namespaces: Iterable[str] = ("meridian",),
        rollout_timeout_seconds: float = 180.0,
        runner: Optional[CommandRunner] = None,
    ) -> None:
        self._kubectl = kubectl
        self._allowed_namespaces = frozenset(allowed_namespaces)
        if not self._allowed_namespaces:
            raise FaultAdapterError("at least one namespace must be allowed")
        self._rollout_timeout_seconds = rollout_timeout_seconds
        self._run = runner or _run_subprocess

    def _contracts(self, scenario: Any) -> tuple[_DeploymentContract, ...]:
        fault = _object(getattr(scenario, "fault", None), "fault")
        if fault.get("adapter") != DEPLOYMENT_ENV_ADAPTER:
            raise FaultAdapterError(
                f"unsupported fault adapter: {fault.get('adapter')!r}"
            )
        declared = fault.get("contracts")
        if not isinstance(declared, (list, tuple)) or not declared:
            raise FaultAdapterError("fault.contracts must be a non-empty list")

        contracts: list[_DeploymentContract] = []
        seen: set[str] = set()
        for index, raw in enumerate(declared):
            contract = _object(raw, f"fault.contracts[{index}]")
            target = contract.get("target")
            match = _DEPLOYMENT_PATH.match(str(contract.get("path", "")))
            if match is None:
                raise FaultAdapterError(
                    "deployment fault path must be /apis/apps/v1/namespaces/"
                    "<namespace>/deployments/<name>"
                )
            if target != f"deployment/{match['name']}":
                raise FaultAdapterError(
                    f"fault target {target!r} does not name the deployment "
                    f"in its path ({match['name']})"
                )
            if match["namespace"] not in self._allowed_namespaces:
                raise FaultAdapterError(
                    f"namespace {match['namespace']!r} is not allowed for faults"
                )
            if target in seen:
                raise FaultAdapterError(f"duplicate fault target: {target}")
            seen.add(target)
            inject = _object(contract.get("inject"), f"fault.contracts[{index}].inject")
            cleanup = _object(
                contract.get("cleanup"), f"fault.contracts[{index}].cleanup"
            )
            if not inject or set(inject) != set(cleanup):
                raise FaultAdapterError(
                    "inject and cleanup payloads must declare the same keys"
                )
            bad = sorted(key for key in inject if not _ENV_NAME.match(str(key)))
            if bad:
                raise FaultAdapterError(f"invalid environment variable names: {bad}")
            contracts.append(
                _DeploymentContract(
                    target=target,
                    namespace=match["namespace"],
                    name=match["name"],
                    inject=dict(inject),
                    cleanup=dict(cleanup),
                )
            )
        return tuple(contracts)

    async def _kubectl_ok(self, *args: str, timeout: float = 30.0) -> str:
        code, stdout, stderr = await self._run([self._kubectl, *args], timeout)
        if code != 0:
            raise FaultAdapterError(
                f"kubectl {' '.join(args[:2])} failed: {stderr.strip() or stdout.strip()}"
            )
        return stdout

    async def _read_env(
        self, namespace: str, name: str
    ) -> tuple[str, dict[str, Optional[str]]]:
        """Return the target container and its literal env (None for valueFrom)."""
        raw = await self._kubectl_ok(
            "get", f"deployment/{name}", "-n", namespace, "-o", "json"
        )
        try:
            containers = json.loads(raw)["spec"]["template"]["spec"]["containers"]
        except (ValueError, KeyError, TypeError) as exc:
            raise FaultAdapterError(f"deployment/{name} has no pod template") from exc
        if len(containers) == 1:
            container = containers[0]
        else:
            named = [item for item in containers if item.get("name") == name]
            if len(named) != 1:
                raise FaultAdapterError(
                    f"deployment/{name} has {len(containers)} containers and "
                    "none is named after it"
                )
            container = named[0]
        env = {
            item["name"]: item.get("value", "") if "valueFrom" not in item else None
            for item in container.get("env") or []
        }
        return container["name"], env

    async def _apply(
        self, namespace: str, name: str, container: str, values: dict[str, str]
    ) -> None:
        await self._kubectl_ok(
            "set", "env", f"deployment/{name}", "-n", namespace, "-c", container,
            *(f"{key}={value}" for key, value in values.items()),
        )
        await self._kubectl_ok(
            "rollout", "status", f"deployment/{name}", "-n", namespace,
            f"--timeout={int(self._rollout_timeout_seconds)}s",
            timeout=self._rollout_timeout_seconds + 30,
        )

    async def _restore(self, lease: DeploymentEnvLease) -> None:
        await self._apply(lease.namespace, lease.name, lease.container, lease.original_values)
        _, env = await self._read_env(lease.namespace, lease.name)
        _assert_values(env, lease.original_values, "cleanup rollout")

    async def _inject_one(
        self, scenario_name: str, contract: _DeploymentContract
    ) -> DeploymentEnvLease:
        container, env = await self._read_env(contract.namespace, contract.name)
        unmanaged = sorted(key for key in contract.inject if env.get(key) is None)
        if unmanaged:
            raise FaultAdapterError(
                f"{contract.target} env {unmanaged} is unset or not a literal "
                "value; the baseline cannot be verified"
            )
        mismatched = {
            key: {"expected": value, "actual": env[key]}
            for key, value in contract.cleanup.items()
            if not _env_matches(env[key], value)
        }
        if mismatched:
            raise FaultAdapterError(f"baseline did not confirm requested values: {mismatched}")

        lease = DeploymentEnvLease(
            scenario=scenario_name,
            target=contract.target,
            namespace=contract.namespace,
            name=contract.name,
            container=container,
            original_values={key: env[key] for key in contract.inject},
            injected_values=dict(contract.inject),
            injected_at=datetime.now(timezone.utc),
        )
        try:
            await self._apply(
                contract.namespace, contract.name, container,
                {key: _env_text(value) for key, value in contract.inject.items()},
            )
            _, applied = await self._read_env(contract.namespace, contract.name)
            wrong = sorted(
                key for key, value in contract.inject.items()
                if not _env_matches(applied.get(key), value)
            )
            if wrong:
                raise FaultAdapterError(f"rollout did not carry the injected env: {wrong}")
        except Exception as exc:
            try:
                await self._restore(lease)
            except Exception as cleanup_exc:
                raise FaultAdapterError(
                    f"fault injection failed and cleanup also failed: {cleanup_exc}"
                ) from exc
            if isinstance(exc, FaultAdapterError):
                raise
            raise FaultAdapterError(f"fault injection failed: {exc}") from exc
        return lease

    async def inject(self, client: Any, scenario: Any) -> tuple[DeploymentEnvLease, ...]:
        """Roll out every declared contract, unwinding all of them on failure."""
        del client  # kubectl, not HTTP; kept for the adapter interface
        contracts = self._contracts(scenario)
        scenario_name = str(getattr(scenario, "name", "unknown"))
        leases: list[DeploymentEnvLease] = []
        for contract in contracts:
            try:
                leases.append(await self._inject_one(scenario_name, contract))
            except Exception as exc:
                try:
                    await self.cleanup(None, leases)
                except Exception as unwind_exc:
                    raise FaultAdapterError(
                        f"fault injection failed on {contract.target} and "
                        f"unwinding earlier contracts also failed: {unwind_exc}"
                    ) from exc
                raise
        return tuple(leases)

    async def cleanup(
        self, client: Any, leases: Sequence[DeploymentEnvLease] | Iterable[DeploymentEnvLease]
    ) -> None:
        """Restore every lease, reporting the first failure only after trying all."""
        del client
        failures: list[str] = []
        for lease in reversed(list(leases)):
            try:
                await self._restore(lease)
            except Exception as exc:
                failures.append(f"{lease.target}: {exc}")
        if failures:
            raise FaultAdapterError(f"fault cleanup failed: {'; '.join(failures)}")


class FaultAdapterRouter:
    """Send each scenario to the adapter its `fault.adapter` names.

    A scenario declares exactly one adapter, so one injection's leases all
    share a type; cleanup routes them back by that type.
    """

    def __init__(self, adapters: Iterable[Any]) -> None:
        self._by_id: dict[str, Any] = {}
        self._by_lease: dict[type, Any] = {}
        for adapter in adapters:
            if adapter.adapter_id in self._by_id:
                raise FaultAdapterError(f"duplicate adapter: {adapter.adapter_id}")
            self._by_id[adapter.adapter_id] = adapter
            self._by_lease[adapter.lease_type] = adapter
        if not self._by_id:
            raise FaultAdapterError("at least one adapter is required")

    async def inject(self, client: Any, scenario: Any) -> tuple[Any, ...]:
        name = _object(getattr(scenario, "fault", None), "fault").get("adapter")
        adapter = self._by_id.get(name)
        if adapter is None:
            raise FaultAdapterError(f"unsupported fault adapter: {name!r}")
        return await adapter.inject(client, scenario)

    async def cleanup(self, client: Any, leases: Iterable[Any]) -> None:
        pending = list(leases)
        failures: list[str] = []
        for lease_type in dict.fromkeys(type(lease) for lease in reversed(pending)):
            adapter = self._by_lease.get(lease_type)
            batch = [lease for lease in pending if type(lease) is lease_type]
            if adapter is None:
                failures.append(f"no adapter restores {lease_type.__name__}")
                continue
            try:
                await adapter.cleanup(client, batch)
            except Exception as exc:
                failures.append(str(exc))
        if failures:
            raise FaultAdapterError("; ".join(failures))
