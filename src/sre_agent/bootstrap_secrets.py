#!/usr/bin/env python3
"""Materialise the three secrets that cannot live in the settings database.

Everything a user configures — provider keys, Slack, Notion, Jira, cluster
endpoints — belongs in the dashboard's Settings, encrypted at rest. Three
values cannot. ``CREDENTIAL_ENCRYPTION_KEY`` is what decrypts that store,
``SECRET_KEY`` signs the session tokens you need in order to reach the settings
page at all, and ``MCP_SERVICE_TOKEN`` authenticates the edge bridges before
any user exists. Storing them in the thing they protect is circular, so they
come from a keystore file on a persisted volume, generated on first boot.

Precedence, per key:

1. a non-empty, non-placeholder value already in the environment — an operator
   supplying their own key, or a secrets manager, always wins;
2. the value recorded in the keystore;
3. a freshly generated value, written to the keystore.

The file is the source of truth rather than the environment because the API
container runs the preflight, the migrations and uvicorn as three separate
processes, and the Temporal worker is a fourth: ``os.environ`` set in one is
invisible to the next, but all four read the same volume.

Deliberately stdlib-only, like ``provider_config``, so it can run at the very
front of an entrypoint — before third-party packages are needed.

**Operational note.** The keystore and the database must be backed up together.
Restoring a database whose keystore is gone leaves every stored credential
undecryptable: ``backend.crypto`` will raise rather than silently return
garbage, but the values are not recoverable. ``SENTINEL_SECRET_STORE`` points
somewhere else when you want that; supplying the keys as environment variables
bypasses this module entirely.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import secrets
import sys
import tempfile
import time
from typing import Callable, Dict, Iterator, Mapping, Optional

DEFAULT_STORE_PATH = "/var/lib/sentinel/secrets.json"
STORE_PATH_ENV = "SENTINEL_SECRET_STORE"
STORE_VERSION = 1

# Substrings that mark a shipped example value rather than a real secret.
# `.env.example` ships SECRET_KEY="sre_platform_dev_key_change_in_production";
# copying that file verbatim is the single most likely first-run path, and
# treating the result as configured would give every such install the same
# token-signing key. Kept in step with provider_config._is_placeholder, but
# duplicated rather than imported: provider_config imports *this* module, and
# this module must stay free of reverse dependencies.
_PLACEHOLDER_MARKERS = (
    "change_in_production",
    "change-in-production",
    "changeme",
    "change_me",
    "change-me",
    "replace_me",
    "replace-me",
    "your_key",
    "your-key",
    "your_api_key",
    "todo",
)


class BootstrapError(RuntimeError):
    """The secret keystore is unusable and no secret could be established."""


def _credential_encryption_key() -> str:
    # backend.crypto._decode_key requires a value that URL-safe-base64 decodes
    # to exactly 32 bytes — an AES-256-GCM key. token_urlsafe(32) would decode
    # to the wrong length and be rejected at first use, so build it explicitly.
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


MANAGED_SECRETS: Dict[str, Callable[[], str]] = {
    "SECRET_KEY": lambda: secrets.token_urlsafe(64),
    "CREDENTIAL_ENCRYPTION_KEY": _credential_encryption_key,
    "MCP_SERVICE_TOKEN": lambda: secrets.token_urlsafe(32),
}


def looks_unset(value: Optional[str]) -> bool:
    """True when a value is missing, blank, or a shipped placeholder."""
    text = (value or "").strip()
    if not text:
        return True
    lowered = text.lower()
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


def store_path(environ: Optional[Mapping[str, str]] = None) -> str:
    source: Mapping[str, str] = environ if environ is not None else os.environ
    return (source.get(STORE_PATH_ENV) or "").strip() or DEFAULT_STORE_PATH


@contextlib.contextmanager
def _exclusive(path: str) -> Iterator[None]:
    """Serialise read-modify-write across concurrently booting containers.

    The API and the Temporal worker start at the same time and share the
    volume. Without this, both could find an empty store, generate different
    keys, and the last writer would win — leaving one process unable to decrypt
    what the other wrote. Advisory locks are POSIX-only; on a platform without
    ``fcntl`` this degrades to no locking, which is the pre-existing situation
    and still correct for the single-process case.
    """
    lock_path = path + ".lock"
    try:
        import fcntl
    except ImportError:
        yield
        return

    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read_store(path: str) -> Dict[str, str]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        # Refuse to guess. A corrupt store that we silently replaced would
        # regenerate the encryption key and orphan every credential in the
        # database, which is far worse than failing to start.
        raise BootstrapError(
            f"Secret keystore {path} exists but could not be read ({exc}). "
            "Refusing to regenerate it: that would orphan every credential "
            "already encrypted in the database. Restore it from backup, or "
            "supply the secrets as environment variables."
        ) from exc

    if not isinstance(data, dict) or not isinstance(data.get("secrets"), dict):
        raise BootstrapError(
            f"Secret keystore {path} is not in the expected format. Refusing "
            "to regenerate it; restore it from backup or supply the secrets "
            "as environment variables."
        )
    return {
        str(name): str(value)
        for name, value in data["secrets"].items()
        if isinstance(value, str) and value.strip()
    }


def _write_store(path: str, values: Dict[str, str]) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    payload = {
        "version": STORE_VERSION,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note": (
            "Generated by sre_agent.bootstrap_secrets. Back this up together "
            "with the database: without it, stored credentials cannot be "
            "decrypted."
        ),
        "secrets": values,
    }

    # Write 0600 from the start — never a window where the file is readable by
    # others — then rename, so a reader sees either the old file or the new one.
    fd, temp_path = tempfile.mkstemp(dir=directory, prefix=".secrets-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_path)
        raise


def ensure_secrets(
    environ: Optional[Dict[str, str]] = None,
    *,
    path: Optional[str] = None,
    allow_ephemeral: bool = True,
) -> Dict[str, str]:
    """Guarantee every managed secret is set in ``environ``.

    Returns a map of secret name to where its value came from — ``"env"``,
    ``"keystore"``, ``"generated"`` or ``"ephemeral"`` — for logging. Never
    returns or logs the values themselves.

    ``allow_ephemeral`` keeps a read-only or unwritable volume from being a
    hard boot failure: keys are generated in memory instead. That is correct
    for tests and ``pytest`` collection, and survivable for a first run with no
    stored credentials yet, but it means tokens break on restart — so it is
    reported loudly rather than quietly.
    """
    target: Dict[str, str] = os.environ if environ is None else environ  # type: ignore[assignment]
    resolved_path = path or store_path(target)
    report: Dict[str, str] = {}

    # Anything the operator supplied wins outright and is never persisted:
    # writing an externally-managed key into our own file would quietly fork
    # it from the secrets manager that owns it.
    needed = [name for name in MANAGED_SECRETS if looks_unset(target.get(name))]
    for name in MANAGED_SECRETS:
        if name not in needed:
            report[name] = "env"

    if not needed:
        return report

    try:
        with _exclusive(resolved_path):
            stored = _read_store(resolved_path)
            generated: Dict[str, str] = {}

            for name in needed:
                existing = stored.get(name)
                if existing and not looks_unset(existing):
                    target[name] = existing
                    report[name] = "keystore"
                else:
                    value = MANAGED_SECRETS[name]()
                    stored[name] = value
                    target[name] = value
                    generated[name] = value
                    report[name] = "generated"

            if generated:
                _write_store(resolved_path, stored)
    except BootstrapError:
        raise
    except OSError as exc:
        if not allow_ephemeral:
            raise BootstrapError(
                f"Cannot write the secret keystore at {resolved_path}: {exc}. "
                "Mount a writable volume there, point SENTINEL_SECRET_STORE "
                "elsewhere, or supply the secrets as environment variables."
            ) from exc
        for name in needed:
            target[name] = MANAGED_SECRETS[name]()
            report[name] = "ephemeral"
        print(
            f"bootstrap-secrets: keystore {resolved_path} is not writable "
            f"({exc}); generated {len(needed)} ephemeral secret(s) in memory. "
            "Sessions will not survive a restart and credentials saved in "
            "Settings will not be readable after one. Mount a writable volume "
            "at that path to fix this permanently.",
            file=sys.stderr,
        )

    return report


def describe(report: Mapping[str, str]) -> str:
    """One-line, value-free summary suitable for a startup log."""
    return "bootstrap-secrets: " + ", ".join(
        f"{name}={source}" for name, source in sorted(report.items())
    )


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry: establish the secrets, print where each came from."""
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        report = ensure_secrets(allow_ephemeral="--strict" not in args)
    except BootstrapError as exc:
        print(f"bootstrap-secrets failed: {exc}", file=sys.stderr)
        return 1
    print(describe(report))
    print(f"keystore: {store_path()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
