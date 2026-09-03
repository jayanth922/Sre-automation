#!/usr/bin/env python3
"""
Skill memory — the self-improving loop.

When a remediation is applied for an incident, it is recorded as a **skill**
keyed by the incident's signature (alert / service / failure class). When a
similar incident recurs, the agent can retrieve the matching skill and
propose "apply the fix that worked last time" instead of re-reasoning from
scratch — and each recurrence increments the skill's success count, so the
most battle-tested fix rises to the top. First-party and independent of the
actor backend (see sre_agent/actor_runtime.py).

This complements the existing Qdrant incident memory (semantic recall of past
incidents); skills are the *actionable* distillation — the concrete remediation
steps that resolved a class of incident.

Pure/stdlib and backend-agnostic (in-memory default) so it is fully testable; a
Qdrant/DB-backed store can be swapped in behind the same interface later.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
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
    "oom": "oom",
    "memory": "oom",
    "imagepull": "imagepull",
    "latency": "latency",
    "slow": "latency",
    "error": "high_error_rate",
    "payment": "dependency",
    "provider": "dependency",
    "dependency": "dependency",
    "saturation": "saturation",
    "cpu": "saturation",
    "deploy": "bad_deploy",
    "rollout": "bad_deploy",
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
    actions: List[Dict[str, Any]]  # [{action_type, target, parameters?}]
    source_incident_id: Optional[str] = None
    success_count: int = 1
    notes: str = ""
    outcome: str = "verified_success"
    verification_status: Optional[str] = None
    reviewer_id: Optional[str] = None
    run_manifest_sha256: Optional[str] = None
    config_fingerprint: Optional[str] = None
    invalidated: bool = False
    invalidation_reason: Optional[str] = None

    def brief(self) -> Dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "failure_class": self.signature.failure_class,
            "service": self.signature.service,
            "actions": [a.get("action_type") for a in self.actions],
            "success_count": self.success_count,
            "outcome": self.outcome,
            "verification_status": self.verification_status,
            "invalidated": self.invalidated,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "signature": {
                "alert_name": self.signature.alert_name,
                "service": self.signature.service,
                "failure_class": self.signature.failure_class,
            },
            "actions": self.actions,
            "source_incident_id": self.source_incident_id,
            "success_count": self.success_count,
            "notes": self.notes,
            "outcome": self.outcome,
            "verification_status": self.verification_status,
            "reviewer_id": self.reviewer_id,
            "run_manifest_sha256": self.run_manifest_sha256,
            "config_fingerprint": self.config_fingerprint,
            "invalidated": self.invalidated,
            "invalidation_reason": self.invalidation_reason,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Skill":
        sig = d.get("signature", {})
        return Skill(
            skill_id=d["skill_id"],
            signature=IncidentSignature(
                sig.get("alert_name", ""),
                sig.get("service", ""),
                sig.get("failure_class", ""),
            ),
            actions=d.get("actions", []),
            source_incident_id=d.get("source_incident_id"),
            success_count=int(d.get("success_count", 1)),
            notes=d.get("notes", ""),
            outcome=str(d.get("outcome", "verified_success") or "verified_success"),
            verification_status=d.get("verification_status"),
            reviewer_id=d.get("reviewer_id"),
            run_manifest_sha256=d.get("run_manifest_sha256"),
            config_fingerprint=d.get("config_fingerprint"),
            invalidated=bool(d.get("invalidated", False)),
            invalidation_reason=d.get("invalidation_reason"),
        )


def signature_from_alert(alert: Any) -> IncidentSignature:
    """Derive an incident signature from an alert context (dict or object)."""
    alert_name = str(
        _get(alert, "alert_name", "")
        or _get(_get(alert, "labels", {}) or {}, "alertname", "")
        or "unknown"
    )
    labels = _get(alert, "labels", {}) or {}
    service = str(labels.get("service") or labels.get("app") or "unknown")
    return IncidentSignature(
        alert_name=alert_name, service=service, failure_class=_failure_class(alert_name)
    )


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
        self._negatives: Dict[str, Dict[str, Any]] = {}

    def add(self, skill: Skill) -> Skill:
        if skill.invalidated or skill.outcome != "verified_success":
            raise ValueError("only verified successful skills may be promoted")
        if (
            not skill.verification_status
            or str(skill.verification_status).upper() != "RESOLVED"
        ):
            raise ValueError("skills require RESOLVED verification provenance")
        if not skill.source_incident_id:
            raise ValueError("skills require a verified source incident_id")
        key = skill.signature.key()
        existing = self._skills.get(key)
        if existing and not existing.invalidated:
            existing.success_count += 1
            existing.actions = skill.actions or existing.actions
            existing.source_incident_id = (
                skill.source_incident_id or existing.source_incident_id
            )
            existing.verification_status = (
                skill.verification_status or existing.verification_status
            )
            existing.reviewer_id = skill.reviewer_id or existing.reviewer_id
            existing.run_manifest_sha256 = (
                skill.run_manifest_sha256 or existing.run_manifest_sha256
            )
            existing.config_fingerprint = (
                skill.config_fingerprint or existing.config_fingerprint
            )
            return existing
        self._skills[key] = skill
        return skill

    def add_negative(self, exemplar: Dict[str, Any]) -> Dict[str, Any]:
        exemplar_id = str(exemplar.get("exemplar_id") or "").strip()
        if not exemplar_id:
            raise ValueError("negative exemplar_id is required")
        self._negatives[exemplar_id] = dict(exemplar)
        return self._negatives[exemplar_id]

    def invalidate(
        self,
        skill_id: str,
        *,
        reason: str,
        evidence: Optional[Dict[str, Any]] = None,
    ) -> Optional[Skill]:
        for skill in self._skills.values():
            if skill.skill_id != skill_id:
                continue
            skill.invalidated = True
            skill.invalidation_reason = reason
            if evidence:
                skill.notes = (
                    f"{skill.notes}\ninvalidated:{json_dumps(evidence)}"
                    if skill.notes
                    else f"invalidated:{json_dumps(evidence)}"
                )
            return skill
        return None

    def find_matching(
        self, signature: IncidentSignature, threshold: float = 0.5
    ) -> List[Tuple[Skill, float]]:
        scored = [
            (s, match_score(signature, s.signature))
            for s in self._skills.values()
            if not s.invalidated and s.outcome == "verified_success"
        ]
        hits = [(s, sc) for s, sc in scored if sc >= threshold]
        hits.sort(key=lambda t: (t[1], t[0].success_count), reverse=True)
        return hits

    def all(self) -> List[Skill]:
        return list(self._skills.values())

    def negatives(self) -> List[Dict[str, Any]]:
        return list(self._negatives.values())


def json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class JsonSkillStore(InMemorySkillStore):
    """Skill store persisted to a JSON file — survives process restarts.

    Same interface as InMemorySkillStore; loads on init and saves on every add.
    A DB/Qdrant-backed store can drop in behind the same interface later.
    """

    def __init__(self, path: str) -> None:
        super().__init__()
        import os

        self.path = path
        self._load()
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)

    def _load(self) -> None:
        import json
        import os

        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if isinstance(payload, list):
                skills = payload
                negatives = []
            elif isinstance(payload, dict):
                skills = payload.get("skills", [])
                negatives = payload.get("negatives", [])
            else:
                return
            for d in skills:
                skill = Skill.from_dict(d)
                self._skills[skill.signature.key()] = skill
            for negative in negatives:
                if isinstance(negative, dict) and negative.get("exemplar_id"):
                    self._negatives[str(negative["exemplar_id"])] = dict(negative)
        except Exception as e:  # corrupt/partial file → start empty, don't crash
            logger.warning(f"SkillStore: could not load {self.path}: {e}")

    def _save(self) -> None:
        import json

        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "skills": [s.to_dict() for s in self._skills.values()],
                        "negatives": list(self._negatives.values()),
                    },
                    f,
                    indent=2,
                )
        except Exception as e:
            logger.warning(f"SkillStore: could not persist {self.path}: {e}")

    def add(self, skill: Skill) -> Skill:
        stored = super().add(skill)
        self._save()
        return stored

    def add_negative(self, exemplar: Dict[str, Any]) -> Dict[str, Any]:
        stored = super().add_negative(exemplar)
        self._save()
        return stored

    def invalidate(
        self,
        skill_id: str,
        *,
        reason: str,
        evidence: Optional[Dict[str, Any]] = None,
    ) -> Optional[Skill]:
        skill = super().invalidate(skill_id, reason=reason, evidence=evidence)
        if skill is not None:
            self._save()
        return skill


_GLOBAL_STORE: Optional[InMemorySkillStore] = None


def get_skill_store() -> InMemorySkillStore:
    """Process store. JSON-backed (durable) when SKILL_STORE_PATH is set, else in-memory."""
    global _GLOBAL_STORE
    if _GLOBAL_STORE is None:
        import os

        path = os.getenv("SKILL_STORE_PATH")
        _GLOBAL_STORE = JsonSkillStore(path) if path else InMemorySkillStore()
    return _GLOBAL_STORE


def skill_from_remediation(
    alert: Any,
    actions: List[Dict[str, Any]],
    incident_id: Optional[str],
    *,
    verification_status: str = "RESOLVED",
    reviewer_id: Optional[str] = None,
    run_manifest_sha256: Optional[str] = None,
    config_fingerprint: Optional[str] = None,
) -> Skill:
    sig = signature_from_alert(alert)
    skill_id = f"skill-{sig.failure_class}-{sig.service}"
    return Skill(
        skill_id=skill_id,
        signature=sig,
        actions=actions,
        source_incident_id=incident_id,
        verification_status=verification_status,
        reviewer_id=reviewer_id,
        run_manifest_sha256=run_manifest_sha256,
        config_fingerprint=config_fingerprint,
        outcome="verified_success",
    )


def record_successful_remediation(
    store: InMemorySkillStore,
    alert: Any,
    executed_actions: List[Dict[str, Any]],
    incident_id: Optional[str] = None,
    *,
    verification_status: str = "RESOLVED",
    reviewer_id: Optional[str] = None,
    run_manifest_sha256: Optional[str] = None,
    config_fingerprint: Optional[str] = None,
) -> Optional[Skill]:
    """Record live-verified remediation actions as a reusable skill."""
    if not executed_actions:
        return None
    skill = skill_from_remediation(
        alert,
        executed_actions,
        incident_id,
        verification_status=verification_status,
        reviewer_id=reviewer_id,
        run_manifest_sha256=run_manifest_sha256,
        config_fingerprint=config_fingerprint,
    )
    stored = store.add(skill)
    logger.info(
        f"🧠 SkillStore: recorded '{stored.skill_id}' (success_count={stored.success_count})"
    )
    return stored


def propose_skills(
    store: InMemorySkillStore, alert: Any, limit: int = 3, threshold: float = 0.5
) -> List[Skill]:
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
