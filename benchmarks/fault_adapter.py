#!/usr/bin/env python3
"""Safe executable adapter for Meridian `/admin/config` fault contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
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

    def _contract(
        self, scenario: Any
    ) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
        fault = _object(getattr(scenario, "fault", None), "fault")
        if fault.get("adapter") != SUPPORTED_ADAPTER:
            raise FaultAdapterError(
                f"unsupported fault adapter: {fault.get('adapter')!r}"
            )
        target = fault.get("target")
        if not isinstance(target, str) or target not in self._service_urls:
            raise FaultAdapterError(f"unsupported fault target: {target!r}")
        inject = _object(fault.get("inject"), "fault.inject")
        cleanup = _object(fault.get("cleanup"), "fault.cleanup")
        path = inject.get("path")
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or path.startswith("//")
            or cleanup.get("path") != path
        ):
            raise FaultAdapterError("inject/cleanup paths must match and be relative")
        inject_values = _object(inject.get("payload"), "fault.inject.payload")
        cleanup_values = _object(cleanup.get("payload"), "fault.cleanup.payload")
        if not inject_values or set(inject_values) != set(cleanup_values):
            raise FaultAdapterError(
                "inject and cleanup payloads must declare the same keys"
            )
        return target, path, inject_values, cleanup_values

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

    async def inject(self, client: Any, scenario: Any) -> FaultLease:
        target, path, inject_values, cleanup_values = self._contract(scenario)
        url = f"{self._service_urls[target]}{path}"
        current_response = await client.get(url, timeout=self._timeout_seconds)
        current = _response_object(current_response, "baseline response")
        original_values = {key: current.get(key) for key in inject_values}
        _assert_values(original_values, cleanup_values, "baseline")

        injected_at = datetime.now(timezone.utc)
        try:
            response = await client.post(
                url,
                json=inject_values,
                timeout=self._timeout_seconds,
            )
            applied = _response_object(response, "injection response")
            _assert_values(applied, inject_values, "injection response")
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
            scenario=str(getattr(scenario, "name", "unknown")),
            target=target,
            url=self._service_urls[target],
            path=path,
            original_values=original_values,
            injected_values=dict(inject_values),
            injected_at=injected_at,
        )

    async def cleanup(self, client: Any, lease: FaultLease) -> None:
        await self._restore(
            client,
            url=f"{lease.url}{lease.path}",
            original_values=lease.original_values,
        )
