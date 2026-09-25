#!/usr/bin/env python3
"""Safe executable adapter for Meridian `/admin/config` fault contracts.

A scenario declares one or more contracts, each naming a service, the config
keys to change, and the baseline those keys must already hold. The adapter
verifies the baseline before touching anything, applies the contracts in the
declared order, and unwinds everything it applied if any later contract fails.
The invariant that matters: a benchmark run never leaves a service in a state
the scenario did not declare, even when injection fails halfway through.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence
from urllib.parse import urlparse

SUPPORTED_ADAPTER = "meridian_admin_config_v1"


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
