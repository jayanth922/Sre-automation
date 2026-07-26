#!/usr/bin/env python3
"""Unit tests for investigation concurrency + sandboxing (interview Q1)."""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "sre_agent" / "concurrency.py"
_spec = importlib.util.spec_from_file_location("concurrency", _MODULE_PATH)
cc = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = cc
_spec.loader.exec_module(cc)


def test_acquire_up_to_capacity_then_reject():
    lim = cc.InvestigationLimiter(max_concurrent=2)
    assert lim.try_acquire("a") is True
    assert lim.try_acquire("b") is True
    assert lim.try_acquire("c") is False  # full
    assert lim.active == 2


def test_acquire_is_idempotent_per_incident():
    lim = cc.InvestigationLimiter(max_concurrent=1)
    assert lim.try_acquire("a") is True
    assert lim.try_acquire("a") is True  # same incident, still 1 slot used
    assert lim.active == 1


def test_release_frees_a_slot():
    lim = cc.InvestigationLimiter(max_concurrent=1)
    lim.try_acquire("a")
    assert lim.try_acquire("b") is False
    lim.release("a")
    assert lim.try_acquire("b") is True


def test_stats():
    lim = cc.InvestigationLimiter(max_concurrent=3)
    lim.try_acquire("a")
    assert lim.stats() == {"active": 1, "capacity": 3, "available": 2}


def test_slot_context_manager_releases():
    lim = cc.InvestigationLimiter(max_concurrent=1)
    with lim.slot("a"):
        assert lim.active == 1
    assert lim.active == 0


def test_slot_raises_at_capacity():
    lim = cc.InvestigationLimiter(max_concurrent=1)
    with lim.slot("a"):
        with pytest.raises(cc.AtCapacityError):
            with lim.slot("b"):
                pass


def test_default_max_from_env(monkeypatch):
    monkeypatch.setenv("MAX_CONCURRENT_INVESTIGATIONS", "9")
    assert cc.InvestigationLimiter().max == 9


def test_sandbox_create_and_cleanup(tmp_path):
    sb = cc.create_sandbox("inc-1", base_dir=str(tmp_path))
    assert os.path.isdir(sb.workspace)
    sb.cleanup()
    assert not os.path.isdir(sb.workspace)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
