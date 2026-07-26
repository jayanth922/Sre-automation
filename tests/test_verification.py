#!/usr/bin/env python3
"""Unit tests for remediation verification."""

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "sre_agent" / "verification.py"
_spec = importlib.util.spec_from_file_location("verification", _MODULE_PATH)
v = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = v
_spec.loader.exec_module(v)


def test_evaluate_resolved_and_failed():
    assert v.evaluate_verification(0.5, 0.02, 0.05).status == "RESOLVED"
    assert v.evaluate_verification(0.5, 0.40, 0.05).status == "FAILED"


def test_evaluate_improvement_pct():
    out = v.evaluate_verification(0.50, 0.10, 0.20)  # 0.10 < 0.20 → resolved
    assert out.status == "RESOLVED"
    assert out.improvement_pct == pytest.approx(80.0)


def test_evaluate_unknown_without_current_or_threshold():
    assert v.evaluate_verification(0.5, None, 0.05).status == "UNKNOWN"
    assert v.evaluate_verification(0.5, 0.02, None).status == "UNKNOWN"


def test_parse_prom_value_instant():
    assert v.parse_prom_value([{"value": [123, "0.037"]}]) == pytest.approx(0.037)


def test_parse_prom_value_range_uses_last():
    assert v.parse_prom_value([{"values": [[1, "0.9"], [2, "0.1"]]}]) == pytest.approx(0.1)


def test_parse_prom_value_json_string():
    assert v.parse_prom_value('[{"value": [1, "0.5"]}]') == pytest.approx(0.5)


def test_parse_prom_value_bad_input_is_none():
    assert v.parse_prom_value("not json") is None
    assert v.parse_prom_value([]) is None


def test_verify_remediation_resolved_via_caller():
    async def caller(tool, args):
        return [{"value": [0, "0.02"]}]

    out = asyncio.run(v.verify_remediation("sum(rate(http_errors_total[5m]))", 0.05, caller))
    assert out.status == "RESOLVED"
    assert out.current_value == pytest.approx(0.02)


def test_verify_remediation_query_failure_is_unknown():
    async def boom(tool, args):
        raise RuntimeError("prometheus down")

    out = asyncio.run(v.verify_remediation("q", 0.05, boom))
    assert out.status == "UNKNOWN"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
