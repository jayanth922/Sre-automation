#!/usr/bin/env python3
"""Unit tests for skill memory (project #2 — the self-improving loop)."""

import importlib.util
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "sre_agent" / "skill_store.py"
_spec = importlib.util.spec_from_file_location("skill_store", _MODULE_PATH)
skill_store = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = skill_store
_spec.loader.exec_module(skill_store)


def _alert(name, service):
    return {"alert_name": name, "labels": {"service": service}}


def test_failure_class_mapping():
    assert skill_store._failure_class("CheckoutHighErrorRate") == "high_error_rate"
    assert skill_store._failure_class("InventoryOOMKilled") == "oom"
    assert skill_store._failure_class("PaymentProviderDown") == "dependency"
    assert skill_store._failure_class("SomethingWeird") == "unknown"


def test_signature_from_alert():
    sig = skill_store.signature_from_alert(_alert("CheckoutHighErrorRate", "checkout-service"))
    assert sig.service == "checkout-service"
    assert sig.failure_class == "high_error_rate"


def test_match_score_same_class_and_service():
    a = skill_store.signature_from_alert(_alert("CheckoutHighErrorRate", "checkout-service"))
    b = skill_store.signature_from_alert(_alert("CheckoutHighErrorRate", "checkout-service"))
    assert skill_store.match_score(a, b) == pytest.approx(1.0)


def test_match_score_partial():
    a = skill_store.signature_from_alert(_alert("CheckoutHighErrorRate", "checkout-service"))
    b = skill_store.signature_from_alert(_alert("OtherHighErrorRate", "inventory-service"))
    # same failure_class only → 0.5
    assert skill_store.match_score(a, b) == pytest.approx(0.5)


def test_store_add_and_merge_increments_success_count():
    store = skill_store.InMemorySkillStore()
    s1 = skill_store.skill_from_remediation(_alert("CheckoutHighErrorRate", "checkout-service"),
                                            [{"action_type": "rollback", "target": "checkout-service"}], "inc-1")
    store.add(s1)
    s2 = skill_store.skill_from_remediation(_alert("CheckoutHighErrorRate", "checkout-service"),
                                            [{"action_type": "rollback", "target": "checkout-service"}], "inc-2")
    merged = store.add(s2)
    assert merged.success_count == 2
    assert len(store.all()) == 1


def test_record_and_propose_across_incidents():
    store = skill_store.InMemorySkillStore()
    # First incident: record a rollback that worked.
    rec = skill_store.record_successful_remediation(
        store, _alert("CheckoutHighErrorRate", "checkout-service"),
        [{"action_type": "rollback", "target": "checkout-service"}], "inc-1",
    )
    assert rec is not None
    # Second, similar incident: the skill is proposed.
    proposed = skill_store.propose_skills(store, _alert("CheckoutHighErrorRate", "checkout-service"))
    assert len(proposed) == 1
    assert proposed[0].actions[0]["action_type"] == "rollback"


def test_record_no_actions_returns_none():
    store = skill_store.InMemorySkillStore()
    assert skill_store.record_successful_remediation(store, _alert("X", "y"), []) is None


def test_propose_returns_empty_when_no_match():
    store = skill_store.InMemorySkillStore()
    skill_store.record_successful_remediation(
        store, _alert("CheckoutHighErrorRate", "checkout-service"),
        [{"action_type": "rollback", "target": "checkout-service"}], "inc-1",
    )
    # Different, unrelated class → no proposal above threshold.
    assert skill_store.propose_skills(store, _alert("WeirdUnknownThing", "other-service")) == []


def test_skill_to_dict_roundtrip():
    s = skill_store.skill_from_remediation(_alert("CheckoutHighErrorRate", "checkout-service"),
                                           [{"action_type": "rollback", "target": "checkout-service"}], "inc-1")
    s2 = skill_store.Skill.from_dict(s.to_dict())
    assert s2.skill_id == s.skill_id
    assert s2.signature.failure_class == "high_error_rate"
    assert s2.actions[0]["action_type"] == "rollback"


def test_json_skill_store_persists_across_instances(tmp_path):
    path = str(tmp_path / "skills.json")
    store1 = skill_store.JsonSkillStore(path)
    skill_store.record_successful_remediation(
        store1, _alert("CheckoutHighErrorRate", "checkout-service"),
        [{"action_type": "rollback", "target": "checkout-service"}], "inc-1",
    )
    # New instance reading the same file sees the skill.
    store2 = skill_store.JsonSkillStore(path)
    proposed = skill_store.propose_skills(store2, _alert("CheckoutHighErrorRate", "checkout-service"))
    assert len(proposed) == 1
    assert proposed[0].actions[0]["action_type"] == "rollback"


def test_json_skill_store_merges_success_count(tmp_path):
    path = str(tmp_path / "skills.json")
    alert = _alert("CheckoutHighErrorRate", "checkout-service")
    actions = [{"action_type": "rollback", "target": "checkout-service"}]
    skill_store.record_successful_remediation(skill_store.JsonSkillStore(path), alert, actions, "inc-1")
    merged = skill_store.record_successful_remediation(skill_store.JsonSkillStore(path), alert, actions, "inc-2")
    assert merged.success_count == 2


def test_format_skills_for_prompt():
    store = skill_store.InMemorySkillStore()
    skill_store.record_successful_remediation(
        store, _alert("CheckoutHighErrorRate", "checkout-service"),
        [{"action_type": "rollback", "target": "checkout-service"}], "inc-1",
    )
    skills = skill_store.propose_skills(store, _alert("CheckoutHighErrorRate", "checkout-service"))
    text = skill_store.format_skills_for_prompt(skills)
    assert "rollback" in text and "worked 1×" in text


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
