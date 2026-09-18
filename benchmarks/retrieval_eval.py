#!/usr/bin/env python3
"""Offline retrieval quality for the verified-skill and incident-memory indexes.

`sre_agent/retrieval_metrics.py` measures the *shape* of every retrieval call
in production — empty rate, availability, latency, score distribution. That
catches a dead index, but it cannot say whether what came back was the right
thing, because production traffic carries no relevance labels.

This is the labeled half, and the only interesting question about it is where
the labels come from. `benchmarks/datasets/README.md` forbids invented ones:
a case must be backed by a runnable fixture, not by somebody's opinion of what
"should" match. So every probe here is **derived** — each label is a
consequence of a contract the code already commits to, and would be wrong only
if that contract were wrong:

* **self-retrieval** — a skill learned from scenario S must come back first
  when scenario S recurs. If it does not, the store is broken, not merely
  imprecise. Label source: the identity of the scenario that produced it.
* **paraphrase** — the same failure class on the same service, under a
  *different* alert name, must still recall it. This is the entire point of
  `skill_store._failure_class`: generalize across differently-named alerts of
  the same underlying kind. The paraphrase name is generated from the keyword
  taxonomy itself, not hand-written, and is discarded unless it genuinely maps
  to the intended class.
* **tenant and cluster isolation** — the same alert under another tenant must
  return nothing. `_find_matching` filters these before scoring; the label is
  the hard boundary, not a judgment call.
* **distractor rejection** — an unrelated failure class on a service that does
  not exist must return nothing.
* **invalidation** — an invalidated skill must stop being recalled.

Precision is reported but deliberately **not gated**. A same-class skill from
a different service scores 0.5 and legitimately enters the candidate list —
that is the taxonomy working as designed, not a defect — so precision below
1.0 is informative rather than failing. What is gated is rank and silence:
the right skill first (`mrr`, `hit_rate`) and nothing at all where nothing
belongs (`false_positive_rate`).

The verified-skill half is pure Python and runs anywhere, CI included. The
incident-memory half needs Qdrant and an embedding model, so it runs only when
`--qdrant-url` is given and is reported as `skipped` otherwise — never as a
pass it did not earn.

Usage::

    python benchmarks/retrieval_eval.py --output reports/release-retrieval.json
    python benchmarks/retrieval_eval.py --output /tmp/r.json \\
        --qdrant-url http://localhost:6333
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

_BENCHMARKS = Path(__file__).resolve().parent
for _path in (str(_BENCHMARKS.parent), str(_BENCHMARKS)):
    # Repo root for `sre_agent.*` and `benchmarks.*`; the benchmarks directory
    # itself because `scenario_dataset` reaches its siblings by flat import.
    if _path not in sys.path:
        sys.path.insert(0, _path)

from benchmarks.scenario_dataset import (  # noqa: E402
    DatasetError,
    ScenarioDataset,
    load_dataset,
)
from sre_agent.retrieval_metrics import (  # noqa: E402
    MEMORY_STORE,
    SKILL_STORE,
    LabeledQuery,
    RetrievalReport,
    aggregate,
    evaluate_query,
)
from sre_agent.skill_store import (  # noqa: E402
    _FAILURE_CLASS_KEYWORDS,
    _failure_class,
    InMemorySkillStore,
    record_successful_remediation,
)

SCHEMA_VERSION = 1

# Probe tenancy. Fixed, obviously-synthetic ids: these must never collide with
# a real tenant, and the isolation probes are only meaningful if the "other"
# tenant is genuinely different from the corpus tenant.
CORPUS_ORG = "retrieval-eval-org-a"
CORPUS_CLUSTER = "retrieval-eval-cluster-a"
OTHER_ORG = "retrieval-eval-org-b"
OTHER_CLUSTER = "retrieval-eval-cluster-b"
LIFECYCLE_ORG = "retrieval-eval-org-invalidated"

NONEXISTENT_SERVICE = "retrieval-eval-service-that-does-not-exist"

DEFAULT_SPLITS: Tuple[str, ...] = ("train", "dev")


class RetrievalEvalError(RuntimeError):
    """The evaluation cannot run in a way that would mean anything."""


# ==========================================================================
# Derived probe construction
# ==========================================================================


def _alert_context(alert_name: str, service: str) -> Dict[str, Any]:
    """The alert shape `skill_store.signature_from_alert` reads."""
    return {"alert_name": alert_name, "labels": {"service": service}}


def paraphrase_alert_name(alert_name: str) -> Optional[str]:
    """A different alert name that the taxonomy maps to the same class.

    Generated from `_FAILURE_CLASS_KEYWORDS` rather than written by hand, and
    returned only after `_failure_class` confirms it lands where intended —
    a probe whose own label is unverified is worse than no probe.
    """
    target_class = _failure_class(alert_name)
    if target_class == "unknown":
        return None
    lowered = (alert_name or "").lower()
    for keyword, failure_class in _FAILURE_CLASS_KEYWORDS.items():
        if failure_class != target_class or keyword in lowered:
            continue
        candidate = f"Paraphrase{keyword.capitalize()}Probe"
        if _failure_class(candidate) == target_class:
            return candidate
    return None


def distractor_alert_names(corpus_classes: Sequence[str]) -> List[str]:
    """Alert names whose class cannot match anything in the corpus.

    Always includes an unknown-class name: `match_score` refuses to award
    class credit for `unknown`, so it scores 0.0 against every stored skill
    regardless of what the corpus happens to contain.
    """
    names = ["DistractorNovelProbe"]
    if _failure_class(names[0]) != "unknown":  # pragma: no cover - defensive
        raise RetrievalEvalError(
            "the unknown-class distractor probe is no longer unknown; the "
            "failure-class taxonomy changed and this probe must be rebuilt"
        )
    present = set(corpus_classes)
    for keyword, failure_class in _FAILURE_CLASS_KEYWORDS.items():
        if failure_class in present:
            continue
        candidate = f"Distractor{keyword.capitalize()}Probe"
        if _failure_class(candidate) == failure_class:
            names.append(candidate)
            break
    return names


@dataclass
class SkillProbe:
    """A labeled query plus the tenant it is issued under."""

    query: LabeledQuery
    alert: Dict[str, Any]
    organization_id: str
    cluster_id: str
    kind: str


@dataclass
class SkillCorpus:
    store: InMemorySkillStore
    probes: List[SkillProbe] = field(default_factory=list)
    skipped: List[Dict[str, str]] = field(default_factory=list)


def build_skill_corpus(scenarios: Sequence[Any]) -> SkillCorpus:
    """Populate a skill store from benchmark scenarios and derive the probes.

    One verified skill per distinct incident signature, recorded through the
    same `record_successful_remediation` path production uses — including its
    provenance checks — so the corpus cannot contain a skill the runtime would
    have refused to promote.

    Two scenarios that raise the same alert on the same service share one
    signature, so the store collapses them into a single skill. Probing once
    per scenario would then measure the same retrieval several times and
    report a corpus wider than it is, so the second and later scenarios on a
    signature are recorded (they legitimately raise its success count) but
    contribute no probes. Scenarios whose ground truth is "take no action"
    have no remediation to learn and are not recorded at all — inventing one
    would put a skill in the store that the runtime would never have promoted.
    """
    store = InMemorySkillStore()
    corpus = SkillCorpus(store=store)
    if not scenarios:
        raise RetrievalEvalError("no scenarios to build a retrieval corpus from")

    recorded: List[Tuple[Any, str, str]] = []  # (scenario, alert_name, skill_id)
    probed_signatures: set[Tuple[str, str]] = set()
    for scenario in scenarios:
        alert_name = str(scenario.alert.get("alertname", "")).strip()
        service = str(
            scenario.alert.get("service") or scenario.ground_truth_service or ""
        ).strip()
        if not alert_name or not service:
            corpus.skipped.append(
                {"scenario": scenario.name, "reason": "alert has no name or service"}
            )
            continue
        actions = [
            {"action_type": action_type, "target": service}
            for action_type in sorted(scenario.expected_action_types)
            if action_type != "escalate"
        ]
        if not actions:
            corpus.skipped.append(
                {
                    "scenario": scenario.name,
                    "reason": (
                        "ground truth proposes no executable remediation, so "
                        "there is no skill to learn"
                    ),
                }
            )
            continue
        skill = record_successful_remediation(
            store,
            _alert_context(alert_name, service),
            actions,
            f"incident-{scenario.name}",
            organization_id=CORPUS_ORG,
            cluster_id=CORPUS_CLUSTER,
        )
        if skill is None:  # pragma: no cover - actions are never empty above
            corpus.skipped.append(
                {"scenario": scenario.name, "reason": "no executable actions"}
            )
            continue
        signature = (alert_name, service)
        if signature in probed_signatures:
            corpus.skipped.append(
                {
                    "scenario": scenario.name,
                    "reason": (
                        f"{alert_name} on {service} is already probed by an "
                        "earlier scenario; one signature, one skill"
                    ),
                }
            )
            continue
        probed_signatures.add(signature)
        recorded.append((scenario, alert_name, skill.skill_id))

    if not recorded:
        raise RetrievalEvalError("no scenario produced a verified skill")

    corpus_classes = [_failure_class(name) for _, name, _ in recorded]

    for scenario, alert_name, skill_id in recorded:
        service = str(
            scenario.alert.get("service") or scenario.ground_truth_service
        ).strip()

        corpus.probes.append(
            SkillProbe(
                query=LabeledQuery(
                    query_id=f"self:{scenario.name}",
                    text=f"{alert_name} on {service}",
                    relevant_ids=[skill_id],
                    organization_id=CORPUS_ORG,
                    cluster_id=CORPUS_CLUSTER,
                    note="the same incident recurs; its learned skill must rank first",
                ),
                alert=_alert_context(alert_name, service),
                organization_id=CORPUS_ORG,
                cluster_id=CORPUS_CLUSTER,
                kind="self",
            )
        )

        paraphrase = paraphrase_alert_name(alert_name)
        if paraphrase is None:
            corpus.skipped.append(
                {
                    "scenario": scenario.name,
                    "reason": (
                        f"no paraphrase derivable for {alert_name!r} "
                        f"(class {_failure_class(alert_name)})"
                    ),
                }
            )
        else:
            corpus.probes.append(
                SkillProbe(
                    query=LabeledQuery(
                        query_id=f"paraphrase:{scenario.name}",
                        text=f"{paraphrase} on {service}",
                        relevant_ids=[skill_id],
                        organization_id=CORPUS_ORG,
                        cluster_id=CORPUS_CLUSTER,
                        note="same failure class and service, different alert name",
                    ),
                    alert=_alert_context(paraphrase, service),
                    organization_id=CORPUS_ORG,
                    cluster_id=CORPUS_CLUSTER,
                    kind="paraphrase",
                )
            )

        corpus.probes.append(
            SkillProbe(
                query=LabeledQuery(
                    query_id=f"tenant:{scenario.name}",
                    text=f"{alert_name} on {service} (other tenant)",
                    expects_empty=True,
                    organization_id=OTHER_ORG,
                    cluster_id=CORPUS_CLUSTER,
                    note="another organization must never be offered this skill",
                ),
                alert=_alert_context(alert_name, service),
                organization_id=OTHER_ORG,
                cluster_id=CORPUS_CLUSTER,
                kind="tenant_isolation",
            )
        )
        corpus.probes.append(
            SkillProbe(
                query=LabeledQuery(
                    query_id=f"cluster:{scenario.name}",
                    text=f"{alert_name} on {service} (other cluster)",
                    expects_empty=True,
                    organization_id=CORPUS_ORG,
                    cluster_id=OTHER_CLUSTER,
                    note="another cluster of the same org is also out of scope",
                ),
                alert=_alert_context(alert_name, service),
                organization_id=CORPUS_ORG,
                cluster_id=OTHER_CLUSTER,
                kind="cluster_isolation",
            )
        )

    for index, distractor in enumerate(distractor_alert_names(corpus_classes)):
        corpus.probes.append(
            SkillProbe(
                query=LabeledQuery(
                    query_id=f"distractor:{index}",
                    text=f"{distractor} on {NONEXISTENT_SERVICE}",
                    expects_empty=True,
                    organization_id=CORPUS_ORG,
                    cluster_id=CORPUS_CLUSTER,
                    note="unrelated failure class on a service with no history",
                ),
                alert=_alert_context(distractor, NONEXISTENT_SERVICE),
                organization_id=CORPUS_ORG,
                cluster_id=CORPUS_CLUSTER,
                kind="distractor",
            )
        )

    # Invalidation lives in its own tenant so it cannot perturb the probes
    # above: the tenant filter is a hard boundary, so a skill here is invisible
    # to every other query in the set.
    lifecycle_scenario, lifecycle_alert, _ = recorded[-1]
    lifecycle_service = str(
        lifecycle_scenario.alert.get("service")
        or lifecycle_scenario.ground_truth_service
    ).strip()
    lifecycle_skill = record_successful_remediation(
        store,
        _alert_context(lifecycle_alert, lifecycle_service),
        [{"action_type": "restart", "target": lifecycle_service}],
        f"incident-{lifecycle_scenario.name}-invalidated",
        organization_id=LIFECYCLE_ORG,
        cluster_id=CORPUS_CLUSTER,
    )
    assert lifecycle_skill is not None
    store.invalidate(
        lifecycle_skill.skill_id,
        reason="retrieval_eval_invalidation_probe",
    )
    corpus.probes.append(
        SkillProbe(
            query=LabeledQuery(
                query_id="invalidated:0",
                text=f"{lifecycle_alert} on {lifecycle_service} (invalidated skill)",
                expects_empty=True,
                organization_id=LIFECYCLE_ORG,
                cluster_id=CORPUS_CLUSTER,
                note="an invalidated skill must stop being recalled",
            ),
            alert=_alert_context(lifecycle_alert, lifecycle_service),
            organization_id=LIFECYCLE_ORG,
            cluster_id=CORPUS_CLUSTER,
            kind="invalidated",
        )
    )
    return corpus


# ==========================================================================
# Evaluation
# ==========================================================================


def evaluate_skill_store(corpus: SkillCorpus, k: int = 5) -> RetrievalReport:
    """Run every probe through the production recall path and score it."""
    from sre_agent.skill_store import signature_from_alert

    results = []
    for probe in corpus.probes:
        signature = signature_from_alert(
            probe.alert,
            organization_id=probe.organization_id,
            cluster_id=probe.cluster_id,
        )
        hits = corpus.store.find_matching(signature)
        retrieved = [skill.skill_id for skill, _ in hits][:k]
        results.append(evaluate_query(probe.query, retrieved, k=k))
    return aggregate(SKILL_STORE, results, k=k)


def evaluate_memory_store(
    scenarios: Sequence[Any],
    *,
    qdrant_url: str,
    collection_name: str,
    k: int = 5,
    score_threshold: float = 0.5,
) -> RetrievalReport:
    """Same probe families against the Qdrant-backed incident memory.

    Writes into a dedicated collection. Refusing the production collection is
    not paranoia: this stores obviously-fake incidents under obviously-fake
    tenants, and a benchmark that contaminates the index it measures is worth
    less than no benchmark.
    """
    from sre_agent.memory_store import INCIDENTS_COLLECTION, MemoryStore

    if collection_name == INCIDENTS_COLLECTION:
        raise RetrievalEvalError(
            f"refusing to write probe incidents into the production collection "
            f"{INCIDENTS_COLLECTION!r}; pass --memory-collection"
        )

    store = MemoryStore(qdrant_url=qdrant_url, collection_name=collection_name)
    if not store.is_available():
        raise RetrievalEvalError(
            f"memory store is not available at {qdrant_url} "
            "(Qdrant unreachable or the embedding model could not load)"
        )

    stored: List[Tuple[Any, str, str]] = []
    for scenario in scenarios:
        incident_id = f"retrieval-eval-{scenario.name}"
        symptoms = " ".join(
            part
            for part in (
                scenario.alert.get("summary", ""),
                scenario.alert.get("description", ""),
            )
            if part
        ).strip()
        if not symptoms:
            continue
        root_cause = ", ".join(scenario.root_cause_keywords) or scenario.name
        resolution = ", ".join(sorted(scenario.expected_action_types)) or "restart"
        ok = store.store_incident(
            incident_id,
            symptoms=symptoms,
            root_cause=root_cause,
            resolution=resolution,
            metadata={"alert_name": scenario.alert.get("alertname", "")},
            organization_id=CORPUS_ORG,
            cluster_id=CORPUS_CLUSTER,
            related_limit=0,
        )
        if ok:
            stored.append((scenario, incident_id, symptoms))

    if not stored:
        raise RetrievalEvalError("no probe incident could be written to the memory store")

    results = []
    for scenario, incident_id, symptoms in stored:
        hits = store.search_similar_incidents(
            symptoms,
            limit=k,
            score_threshold=score_threshold,
            organization_id=CORPUS_ORG,
            cluster_id=CORPUS_CLUSTER,
        )
        results.append(
            evaluate_query(
                LabeledQuery(
                    query_id=f"self:{scenario.name}",
                    text=symptoms,
                    relevant_ids=[incident_id],
                    note="an incident is queried with its own symptom text",
                ),
                [hit.get("incident_id", "") for hit in hits],
                k=k,
            )
        )
        leaked = store.search_similar_incidents(
            symptoms,
            limit=k,
            score_threshold=score_threshold,
            organization_id=OTHER_ORG,
            cluster_id=CORPUS_CLUSTER,
        )
        results.append(
            evaluate_query(
                LabeledQuery(
                    query_id=f"tenant:{scenario.name}",
                    text=symptoms,
                    expects_empty=True,
                    note="another organization must never see this incident",
                ),
                [hit.get("incident_id", "") for hit in leaked],
                k=k,
            )
        )

    return aggregate(MEMORY_STORE, results, k=k)


# ==========================================================================
# Gate
# ==========================================================================


@dataclass
class Thresholds:
    minimum_mrr: float = 1.0
    minimum_hit_rate: float = 1.0
    maximum_false_positive_rate: float = 0.0

    def as_dict(self) -> Dict[str, float]:
        return {
            "minimum_mrr": self.minimum_mrr,
            "minimum_hit_rate": self.minimum_hit_rate,
            "maximum_false_positive_rate": self.maximum_false_positive_rate,
        }


def check_thresholds(report: RetrievalReport, thresholds: Thresholds) -> List[str]:
    """Reasons this report fails the gate. Empty means it passes."""
    reasons = []
    if report.scored_queries and report.mrr < thresholds.minimum_mrr:
        reasons.append(
            f"{report.store}: mrr {report.mrr:.4f} is below "
            f"{thresholds.minimum_mrr:.4f}"
        )
    if report.scored_queries and report.hit_rate < thresholds.minimum_hit_rate:
        reasons.append(
            f"{report.store}: hit_rate {report.hit_rate:.4f} is below "
            f"{thresholds.minimum_hit_rate:.4f}"
        )
    if (
        report.empty_probe_queries
        and report.false_positive_rate > thresholds.maximum_false_positive_rate
    ):
        reasons.append(
            f"{report.store}: false_positive_rate {report.false_positive_rate:.4f} "
            f"exceeds {thresholds.maximum_false_positive_rate:.4f}"
        )
    if not report.scored_queries and not report.empty_probe_queries:
        reasons.append(f"{report.store}: no probes ran")
    return reasons


# ==========================================================================
# CLI
# ==========================================================================


def load_scenarios(
    dataset_root: Path, dataset_version_dir: str, splits: Sequence[str]
) -> Tuple[List[Any], List[ScenarioDataset]]:
    """Load the content-addressed splits the probes are derived from.

    `load_dataset` verifies each split's sha256 against the index, so a corpus
    that drifted cannot quietly change what these numbers mean. Holdout is not
    offered: tuning retrieval against it is exactly the use the dataset README
    prohibits.
    """
    loaded = []
    scenarios: List[Any] = []
    for split in splits:
        if split == "holdout":
            raise RetrievalEvalError(
                "the holdout split is not available to retrieval evaluation"
            )
        try:
            dataset = load_dataset(dataset_root, dataset_version_dir, split)
        except DatasetError as exc:
            raise RetrievalEvalError(f"dataset split {split} is not usable: {exc}") from exc
        loaded.append(dataset)
        scenarios.extend(dataset.scenarios)
    return scenarios, loaded


def build_report(
    *,
    dataset_root: Path,
    dataset_version_dir: str,
    splits: Sequence[str],
    k: int,
    thresholds: Thresholds,
    qdrant_url: Optional[str] = None,
    memory_collection: str = "sre_retrieval_eval_v1",
    memory_threshold: float = 0.5,
) -> Dict[str, Any]:
    scenarios, datasets = load_scenarios(dataset_root, dataset_version_dir, splits)
    corpus = build_skill_corpus(scenarios)
    skill_report = evaluate_skill_store(corpus, k=k)

    stores: Dict[str, Any] = {SKILL_STORE: skill_report.as_dict()}
    reasons = check_thresholds(skill_report, thresholds)

    if qdrant_url:
        try:
            memory_report = evaluate_memory_store(
                scenarios,
                qdrant_url=qdrant_url,
                collection_name=memory_collection,
                k=k,
                score_threshold=memory_threshold,
            )
        except RetrievalEvalError as exc:
            stores[MEMORY_STORE] = {"status": "error", "reason": str(exc)}
            reasons.append(f"{MEMORY_STORE}: {exc}")
        else:
            stores[MEMORY_STORE] = memory_report.as_dict()
            reasons.extend(check_thresholds(memory_report, thresholds))
    else:
        # Reported, never counted. A skipped store that silently reads as a
        # pass is how an index stays broken through a green release.
        stores[MEMORY_STORE] = {
            "status": "skipped",
            "reason": "no --qdrant-url; the embedded memory index was not evaluated",
        }

    probe_kinds: Dict[str, int] = {}
    for probe in corpus.probes:
        probe_kinds[probe.kind] = probe_kinds.get(probe.kind, 0) + 1

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": {
            "version": datasets[0].dataset_version if datasets else None,
            "directory": dataset_version_dir,
            "splits": [
                {"split": d.split, "sha256": d.sha256, "scenarios": len(d.scenarios)}
                for d in datasets
            ],
        },
        "k": k,
        "label_provenance": (
            "derived: self-retrieval, taxonomy-generated paraphrase, tenant and "
            "cluster isolation, distractor rejection, invalidation. No hand-"
            "assigned relevance judgments."
        ),
        "probes": probe_kinds,
        "skipped_probes": corpus.skipped,
        "thresholds": thresholds.as_dict(),
        "gated_metrics": ["mrr", "hit_rate", "false_positive_rate"],
        "ungated_metrics": {
            "precision_at_k": (
                "reported only: a same-class skill from another service scores "
                "0.5 by design and legitimately enters the candidate list"
            )
        },
        "stores": stores,
        "status": "PASS" if not reasons else "FAIL",
        "reasons": reasons,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate verified-skill and incident-memory retrieval quality"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dataset-root", type=Path, default=Path("benchmarks/datasets"))
    parser.add_argument("--dataset-version", default="v2", help="dataset directory name")
    parser.add_argument(
        "--splits",
        nargs="+",
        default=list(DEFAULT_SPLITS),
        help="dataset splits to derive the corpus from (holdout is refused)",
    )
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--minimum-mrr", type=float, default=1.0)
    parser.add_argument("--minimum-hit-rate", type=float, default=1.0)
    parser.add_argument("--maximum-false-positive-rate", type=float, default=0.0)
    parser.add_argument(
        "--qdrant-url",
        help="evaluate the incident-memory index too; skipped when omitted",
    )
    parser.add_argument("--memory-collection", default="sre_retrieval_eval_v1")
    parser.add_argument("--memory-threshold", type=float, default=0.5)
    parser.add_argument(
        "--no-fail",
        action="store_true",
        help="write the report and exit 0 even when the gate fails",
    )
    args = parser.parse_args(argv)

    try:
        report = build_report(
            dataset_root=args.dataset_root,
            dataset_version_dir=args.dataset_version,
            splits=args.splits,
            k=args.k,
            thresholds=Thresholds(
                minimum_mrr=args.minimum_mrr,
                minimum_hit_rate=args.minimum_hit_rate,
                maximum_false_positive_rate=args.maximum_false_positive_rate,
            ),
            qdrant_url=args.qdrant_url,
            memory_collection=args.memory_collection,
            memory_threshold=args.memory_threshold,
        )
    except RetrievalEvalError as exc:
        print(f"retrieval evaluation failed: {exc}", file=sys.stderr)
        return 2

    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    args.output.write_text(payload, encoding="utf-8")
    print(f"{report['status']} — {args.output} (sha256 {_sha256(payload)})")
    for reason in report["reasons"]:
        print(f"  - {reason}", file=sys.stderr)
    if report["status"] != "PASS" and not args.no_fail:
        return 1
    return 0


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
