"""Fail-closed runtime identity checks for the shared API/Temporal image.

The API and Temporal worker are intentionally two entrypoints into one image.
This module gives that contract a concrete identity: Docker writes a manifest
after copying the Python sources, both processes verify it before serving work,
and the deploy checker compares the two running identities.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

MANIFEST_SCHEMA_VERSION = 1
MANIFEST_ENV = "SENTINEL_RUNTIME_MANIFEST"
CODE_SHA_ENV = "SENTINEL_CODE_SHA"
_RUNTIME_PACKAGES = ("backend", "sre_agent")
_REQUIRED_SHARED_SYMBOLS = (
    ("sre_agent.act_phase", "execute_live_action_request"),
    ("sre_agent.executor", "NON_MUTATING_ACTIONS"),
    ("sre_agent.incident_remediation_workflow", "ACTIVITIES"),
    ("sre_agent.incident_remediation_workflow", "IncidentRemediationWorkflow"),
    ("sre_agent.incident_remediation_workflow", "LiveRemediationWorkflow"),
    ("sre_agent.sandbox_workflow", "CodeFixVerificationWorkflow"),
)


class RuntimePreflightError(RuntimeError):
    """The running files do not match the image/deployment contract."""


@dataclass(frozen=True)
class RuntimeIdentity:
    role: str
    code_sha: str
    fingerprint: str
    file_count: int
    manifest_path: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _runtime_files(root: Path) -> Iterable[Path]:
    for package in _RUNTIME_PACKAGES:
        package_root = root / package
        if not package_root.is_dir():
            raise RuntimePreflightError(f"runtime package is missing: {package_root}")
        yield from sorted(
            path
            for path in package_root.rglob("*.py")
            if "__pycache__" not in path.parts
        )


def compute_runtime_fingerprint(root: Path) -> tuple[str, int]:
    """Hash runtime paths and bytes in a deterministic order."""
    digest = hashlib.sha256()
    count = 0
    for path in _runtime_files(root):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
        count += 1
    return digest.hexdigest(), count


def write_runtime_manifest(
    path: Path,
    *,
    root: Optional[Path] = None,
    code_sha: Optional[str] = None,
) -> dict[str, Any]:
    runtime_root = (root or Path(__file__).resolve().parents[1]).resolve()
    fingerprint, file_count = compute_runtime_fingerprint(runtime_root)
    payload = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "code_sha": str(code_sha or os.getenv(CODE_SHA_ENV) or "unknown"),
        "fingerprint": fingerprint,
        "file_count": file_count,
    }
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def verify_required_imports() -> None:
    """Import the exact symbols the shared Temporal worker must register."""
    for module_name, symbol_name in _REQUIRED_SHARED_SYMBOLS:
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            raise RuntimePreflightError(
                f"required runtime module failed to import: {module_name}: {exc}"
            ) from exc
        if not hasattr(module, symbol_name):
            raise RuntimePreflightError(
                f"required runtime symbol is missing: {module_name}.{symbol_name}"
            )


def verify_runtime(
    role: str,
    *,
    root: Optional[Path] = None,
    manifest_path: Optional[Path] = None,
) -> RuntimeIdentity:
    """Verify source integrity and worker reachability before accepting work."""
    if role not in {"api", "worker"}:
        raise RuntimePreflightError(f"unknown runtime role: {role}")

    runtime_root = (root or Path(__file__).resolve().parents[1]).resolve()
    current_fingerprint, current_count = compute_runtime_fingerprint(runtime_root)
    configured_manifest = manifest_path
    if configured_manifest is None and os.getenv(MANIFEST_ENV):
        configured_manifest = Path(os.environ[MANIFEST_ENV])

    code_sha = str(os.getenv(CODE_SHA_ENV) or "unknown")
    if configured_manifest is not None:
        if not configured_manifest.is_file():
            raise RuntimePreflightError(
                f"runtime manifest is missing: {configured_manifest}"
            )
        try:
            manifest = json.loads(configured_manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimePreflightError(
                f"runtime manifest is unreadable: {configured_manifest}: {exc}"
            ) from exc
        if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
            raise RuntimePreflightError("runtime manifest schema is unsupported")
        if manifest.get("fingerprint") != current_fingerprint:
            raise RuntimePreflightError(
                "runtime files differ from the image manifest; rebuild both API and worker"
            )
        if manifest.get("file_count") != current_count:
            raise RuntimePreflightError("runtime manifest file count does not match")
        manifest_sha = str(manifest.get("code_sha") or "unknown")
        if code_sha != manifest_sha:
            raise RuntimePreflightError(
                f"runtime code revision mismatch: image={manifest_sha} deployment={code_sha}"
            )
        code_sha = manifest_sha

    if role == "worker":
        verify_required_imports()
    return RuntimeIdentity(
        role=role,
        code_sha=code_sha,
        fingerprint=current_fingerprint,
        file_count=current_count,
        manifest_path=str(configured_manifest) if configured_manifest else None,
    )


def compare_runtime_identities(
    api: Mapping[str, Any], worker: Mapping[str, Any]
) -> None:
    """Require two running entrypoints to report the same built runtime."""
    for key in ("code_sha", "fingerprint", "file_count"):
        if api.get(key) != worker.get(key):
            raise RuntimePreflightError(
                f"API/worker runtime mismatch for {key}: "
                f"api={api.get(key)!r} worker={worker.get(key)!r}"
            )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=("api", "worker"))
    parser.add_argument("--write-manifest", type=Path)
    parser.add_argument("--json", action="store_true")
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.write_manifest:
        payload = write_runtime_manifest(args.write_manifest)
        if args.json:
            print(json.dumps(payload, sort_keys=True))
        return
    if not args.role:
        raise SystemExit("--role is required unless --write-manifest is used")
    try:
        identity = verify_runtime(args.role)
    except RuntimePreflightError as exc:
        raise SystemExit(f"runtime preflight failed: {exc}") from exc
    if args.json:
        print(json.dumps(identity.to_dict(), sort_keys=True))
    else:
        print(
            "runtime preflight passed: "
            f"role={identity.role} code_sha={identity.code_sha} "
            f"fingerprint={identity.fingerprint} files={identity.file_count}"
        )


if __name__ == "__main__":
    main()
