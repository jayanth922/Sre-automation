#!/usr/bin/env python3
"""
Investigation concurrency + sandboxing (interview Q1: architecture & scale).

If 100 incidents fire at once you need ~100 isolated investigation contexts, and
you must bound how many run concurrently and spin their sandboxes up and down.
This module provides that control plane:

- `InvestigationLimiter` — a bounded slot allocator (one slot per concurrent
  investigation). Reject-when-full so the caller can shed load / queue instead
  of overrunning the box.
- `Sandbox` — a per-investigation isolated workspace (its own temp dir), created
  on start and cleaned up on finish.

Pure/stdlib and testable. Wire `slot()` around the per-incident investigation in
the runtime to enforce the cap.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional, Set

logger = logging.getLogger(__name__)


class AtCapacityError(RuntimeError):
    """Raised when no investigation slot is available."""


def default_max_concurrent() -> int:
    try:
        return max(1, int(os.getenv("MAX_CONCURRENT_INVESTIGATIONS", "5")))
    except ValueError:
        return 5


class InvestigationLimiter:
    """Bounded, idempotent slot allocator keyed by incident_id."""

    def __init__(self, max_concurrent: Optional[int] = None) -> None:
        self.max = max_concurrent if max_concurrent is not None else default_max_concurrent()
        self._active: Set[str] = set()

    def try_acquire(self, incident_id: str) -> bool:
        if incident_id in self._active:
            return True  # idempotent: re-entering the same incident is fine
        if len(self._active) >= self.max:
            return False
        self._active.add(incident_id)
        return True

    def release(self, incident_id: str) -> None:
        self._active.discard(incident_id)

    @property
    def active(self) -> int:
        return len(self._active)

    def stats(self) -> Dict[str, int]:
        return {"active": len(self._active), "capacity": self.max, "available": max(0, self.max - len(self._active))}

    @contextmanager
    def slot(self, incident_id: str):
        """Reserve a slot for the duration of the block; reject if at capacity."""
        if not self.try_acquire(incident_id):
            raise AtCapacityError(
                f"at capacity ({self.max} concurrent investigations); shed or queue incident {incident_id}"
            )
        try:
            yield
        finally:
            self.release(incident_id)


_GLOBAL_LIMITER: Optional[InvestigationLimiter] = None


def get_limiter() -> InvestigationLimiter:
    global _GLOBAL_LIMITER
    if _GLOBAL_LIMITER is None:
        _GLOBAL_LIMITER = InvestigationLimiter()
    return _GLOBAL_LIMITER


@dataclass
class Sandbox:
    incident_id: str
    workspace: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def cleanup(self) -> None:
        shutil.rmtree(self.workspace, ignore_errors=True)


def create_sandbox(incident_id: str, base_dir: Optional[str] = None) -> Sandbox:
    """Create an isolated per-investigation workspace directory."""
    base = base_dir or os.getenv("SANDBOX_BASE_DIR") or tempfile.gettempdir()
    os.makedirs(base, exist_ok=True)
    workspace = tempfile.mkdtemp(prefix=f"inv-{incident_id}-", dir=base)
    logger.info(f"🧰 Sandbox created for {incident_id}: {workspace}")
    return Sandbox(incident_id=incident_id, workspace=workspace)
