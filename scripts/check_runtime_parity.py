#!/usr/bin/env python3
"""Verify the running API and Temporal worker use one intact runtime image."""

from __future__ import annotations

import argparse
import json
import subprocess
from typing import Any

_PYTHON = "/app/.venv/bin/python"


class RuntimeParityError(RuntimeError):
    """One container failed preflight or reported a different identity."""


def _identity(container: str, role: str) -> dict[str, Any]:
    command = [
        "docker",
        "exec",
        "-w",
        "/app",
        container,
        _PYTHON,
        "-m",
        "sre_agent.runtime_preflight",
        "--role",
        role,
        "--json",
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeParityError(f"{container} preflight failed: {detail}")
    try:
        return json.loads(completed.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise RuntimeParityError(
            f"{container} returned no valid runtime identity"
        ) from exc


def _compare(api: dict[str, Any], worker: dict[str, Any]) -> None:
    for key in ("code_sha", "fingerprint", "file_count"):
        if api.get(key) != worker.get(key):
            raise RuntimeParityError(
                f"API/worker runtime mismatch for {key}: "
                f"api={api.get(key)!r} worker={worker.get(key)!r}"
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-container", default="sre-agent-api")
    parser.add_argument("--worker-container", default="sre-temporal-worker")
    args = parser.parse_args()

    api = _identity(args.api_container, "api")
    worker = _identity(args.worker_container, "worker")
    _compare(api, worker)
    print(
        "runtime parity passed: "
        f"code_sha={api['code_sha']} fingerprint={api['fingerprint']} "
        f"files={api['file_count']}"
    )


if __name__ == "__main__":
    try:
        main()
    except RuntimeParityError as exc:
        raise SystemExit(f"runtime parity failed: {exc}") from exc
