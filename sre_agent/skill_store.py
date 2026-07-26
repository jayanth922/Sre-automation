#!/usr/bin/env python3
"""
Skill memory — the self-improving loop (project #2: Hermes/OpenClaw).

Hermes's headline feature is that it *saves every workflow it learns as a
reusable skill*, so its capability compounds over time. This module brings that
to the SRE agent: when a remediation is applied for an incident, it is recorded
as a **skill** keyed by the incident's signature (alert / service / failure
class). When a similar incident recurs, the agent can retrieve the matching
skill and propose "apply the fix that worked last time" instead of re-reasoning
from scratch — and each recurrence increments the skill's success count, so the
most battle-tested fix rises to the top.

This complements the existing Qdrant incident memory (semantic recall of past
incidents); skills are the *actionable* distillation — the concrete remediation
steps that resolved a class of incident.

Pure/stdlib and backend-agnostic (in-memory default) so it is fully testable; a
Qdrant/DB-backed store can be swapped in behind the same interface later.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


# Map alert-name keywords to a coarse failure class (the taxonomy in
# docs/ACT_PHASE_DESIGN.md §6). This is what lets a skill generalize across
# differently-named alerts of the same underlying kind.
_FAILURE_CLASS_KEYWORDS = {
    "crashloop": "crashloop",
    "oom": "oom", "memory": "oom",
    "imagepull": "imagepull",
    "latency": "latency", "slow": "latency",
    "error": "high_error_rate",
    "payment": "dependency", "provider": "dependency", "dependency": "dependency",
    "saturation": "saturation", "cpu": "saturation",
    "deploy": "bad_deploy", "rollout": "bad_deploy",
}


def _failure_class(alert_name: str) -> str:
    n = (alert_name or "").lower()
    for kw, cls in _FAILURE_CLASS_KEYWORDS.items():
        if kw in n:
            return cls
    return "unknown"


@dataclass(frozen=True)
class IncidentSignature:
    alert_name: str
    service: str
    failure_class: str

    def key(self) -> str:
        return f"{self.alert_name.lower()}|{self.service.lower()}|{self.failure_class}"


@dataclass
class Skill:
    skill_id: str
    signature: IncidentSignature
    actions: List[Dict[str, Any]]           # [{action_type, target, parameters?}]
    source_incident_id: Optional[str] = None
    success_count: int = 1
    notes: str = ""

    def brief(self) -> Dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "failure_class": self.signature.failure_class,
            "service": self.signature.service,
            "actions": [a.get("action_type") for a in self.actions],
            "success_count": self.success_count,
        }


def signature_from_alert(alert: Any) -> IncidentSignature:
    """Derive an incident signature from an alert context (dict or object)."""
    alert_name = str(_get(alert, "alert_name", "") or _get(_get(alert, "labels", {}) or {}, "alertname", "") or "unknown")
    labels = _get(alert, "labels", {}) or {}
    service = str(labels.get("service") or labels.get("app") or "unknown")
    return IncidentSignature(alert_name=alert_name, service=service, failure_class=_failure_class(alert_name))


def match_score(a: IncidentSignature, b: IncidentSignature) -> float:
    """Similarity in [0,1]: failure class dominates, then service, then exact name."""
    score = 0.0
    if a.failure_class == b.failure_class and a.failure_class != "unknown":
        score += 0.5
    if a.service == b.service and a.service != "unknown":
        score += 0.3
    if a.alert_name.lower() == b.alert_name.lower():
        score += 0.2
    return score


class InMemorySkillStore:
    """Process-local skill store. Merges recurrences (success_count compounds)."""

    def __init__(self) -> None:
        self._skills: Dict[str, Skill] = {}

    def add(self, skill: Skill) -> Skill:
        key = skill.signature.key()
        existing = self._skills.get(key)
        if existing:
            existing.success_count += 1
            existing.actions = skill.actions or existing.actions
            existing.source_incident_id = skill.source_incident_id or existing.source_incident_id
            return existing
        self._skills[key] = skill
        return skill

    def find_matching(self, signature: IncidentSignature, threshold: float = 0.5) -> List[Tuple[Skill, float]]:
        scored = [(s, match_score(signature, s.signature)) for s in self._skills.values()]
        hits = [(s, sc) for s, sc in scored if sc >= threshold]
        hits.sort(key=lambda t: (t[1], t[0].success_count), reverse=True)
        return hits

    def all(self) -> List[Skill]:
        return list(self._skills.values())


_GLOBAL_STORE: Optional[InMemorySkillStore] = None


def get_skill_store() -> InMemorySkillStore:
    global _GLOBAL_STORE
    if _GLOBAL_STORE is None:
        _GLOBAL_STORE = InMemorySkillStore()
    return _GLOBAL_STORE


def skill_from_remediation(alert: Any, actions: List[Dict[str, Any]], incident_id: Optional[str]) -> Skill:
    sig = signature_from_alert(alert)
    skill_id = f"skill-{sig.failure_class}-{sig.service}"
    return Skill(skill_id=skill_id, signature=sig, actions=actions, source_incident_id=incident_id)


def record_successful_remediation(store: InMemorySkillStore, alert: Any, executed_actions: List[Dict[str, Any]],
                                  incident_id: Optional[str] = None) -> Optional[Skill]:
    """Record the actions that remediated an incident as a reusable skill."""
    if not executed_actions:
        return None
    skill = skill_from_remediation(alert, executed_actions, incident_id)
    stored = store.add(skill)
    logger.info(f"🧠 SkillStore: recorded '{stored.skill_id}' (success_count={stored.success_count})")
    return stored


def propose_skills(store: InMemorySkillStore, alert: Any, limit: int = 3, threshold: float = 0.5) -> List[Skill]:
    """Retrieve prior skills that match the current incident, best-first."""
    sig = signature_from_alert(alert)
    return [s for s, _ in store.find_matching(sig, threshold=threshold)][:limit]


def format_skills_for_prompt(skills: List[Skill]) -> str:
    if not skills:
        return ""
    lines = ["## 🧠 Learned skills for this incident class (apply if still valid):"]
    for s in skills:
        acts = ", ".join(a.get("action_type", "?") for a in s.actions)
        lines.append(f"- **{s.skill_id}** (worked {s.success_count}×): {acts}")
    return "\n".join(lines) + "\n"
