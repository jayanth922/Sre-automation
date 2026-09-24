#!/usr/bin/env python3
"""Where does cosine actually divide "same failure class" from "not"?

`SemanticSkillStore` admits a semantically-recalled skill on cosine similarity,
and for a while it tested that cosine against `find_matching(threshold=0.5)` —
a number that belongs to `match_score`, which is additive (0.5 failure class +
0.3 service + 0.2 alert name) and therefore means exactly "same failure class".
Cosine over `signature_text()` only shares the 0..1 range with it. Reusing the
number admitted every unrelated skill in the corpus.

Replacing one guessed constant with another would repeat the mistake, so
`_SEMANTIC_MATCH_FLOOR` is measured instead: embed every v2 benchmark signature
with the production model, score all pairs, and look at where the two
populations sit. This script is that measurement, kept runnable so the constant
can be re-derived rather than trusted — it fails if the separation it assumes
no longer holds, which is what a changed embedding model or a changed
`signature_text()` would look like.

Costs nothing to run: the embedding model is local and no LLM is called.

    .venv/bin/python benchmarks/calibrate_semantic_floor.py
"""

import argparse
import itertools
import json
import sys
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sre_agent.skill_store import (  # noqa: E402
    _SEMANTIC_MATCH_FLOOR,
    IncidentSignature,
    match_score,
    signature_from_alert,
    signature_text,
)

# Any tenant works — the pair scores are tenant-independent, and these keep the
# signatures from being filtered out before they are compared.
ORG_ID = "calibration-org"
CLUSTER_ID = "calibration-cluster"

CANDIDATE_CUTOFFS = (0.50, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95)


def load_signatures(root: Path, splits: Sequence[str]) -> Dict[str, IncidentSignature]:
    """Build signatures the way the runtime does, not the way the dataset stores them.

    The dataset holds `alert.alertname` / `alert.service` flat; the runtime
    reads `alert_name` and `labels.service`. Going through
    `signature_from_alert` keeps `failure_class` derived by the production
    classifier rather than assumed here.
    """
    signatures: Dict[str, IncidentSignature] = {}
    for split in splits:
        path = root / f"{split}.json"
        if not path.exists():
            continue
        document = json.loads(path.read_text(encoding="utf-8"))
        for scenario in document.get("scenarios", []):
            alert = scenario.get("alert") or {}
            probe = {
                "alert_name": alert.get("alertname"),
                "labels": {"service": alert.get("service") or "unknown"},
            }
            signatures[scenario["id"]] = signature_from_alert(
                probe, organization_id=ORG_ID, cluster_id=CLUSTER_ID
            )
    return signatures


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = (sum(x * x for x in a) ** 0.5) * (sum(y * y for y in b) ** 0.5)
    return dot / norm if norm else 0.0


def summarise(label: str, scores: List[float]) -> None:
    if not scores:
        print(f"  {label:32s} (none)")
        return
    ordered = sorted(scores)
    n = len(ordered)
    print(
        f"  {label:32s} n={n:4d}  min={ordered[0]:.3f}  "
        f"p50={ordered[n // 2]:.3f}  max={ordered[-1]:.3f}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset-root",
        default=str(Path(__file__).resolve().parent / "datasets" / "v2"),
        help="directory holding the split JSON files (default: benchmarks/datasets/v2)",
    )
    parser.add_argument(
        "--splits",
        nargs="+",
        default=["train", "dev", "holdout"],
        help="splits to pool; pairs are formed across the pool",
    )
    args = parser.parse_args()

    signatures = load_signatures(Path(args.dataset_root), args.splits)
    if len(signatures) < 2:
        print(f"need at least 2 signatures, found {len(signatures)}")
        return 1

    from sre_agent.embedding import embed_text

    vectors = {name: embed_text(signature_text(sig)) for name, sig in signatures.items()}

    same: List[float] = []
    different: List[float] = []
    admitted_but_unrelated: List[Tuple[float, str, str]] = []
    for (name_a, sig_a), (name_b, sig_b) in itertools.combinations(sorted(signatures.items()), 2):
        score = cosine(vectors[name_a], vectors[name_b])
        shares_class = (
            sig_a.failure_class == sig_b.failure_class and sig_a.failure_class != "unknown"
        )
        (same if shares_class else different).append(score)
        if not shares_class and score >= _SEMANTIC_MATCH_FLOOR:
            admitted_but_unrelated.append((score, name_a, name_b))

    classes = sorted({s.failure_class for s in signatures.values()})
    total = len(same) + len(different)
    print(f"{len(signatures)} signatures, {total} pairs, failure classes: {classes}\n")
    print("cosine over signature_text(), by whether the pair shares a failure class")
    summarise("same failure class", same)
    summarise("different failure class", different)

    print("\nadmission at each candidate cutoff")
    for cutoff in CANDIDATE_CUTOFFS:
        wrong = sum(1 for s in different if s >= cutoff)
        kept = sum(1 for s in same if s >= cutoff)
        marker = "  <-- _SEMANTIC_MATCH_FLOOR" if abs(cutoff - _SEMANTIC_MATCH_FLOOR) < 1e-9 else ""
        print(
            f"  {cutoff:.2f}  wrong-class admitted {wrong:4d}/{len(different):<5d}"
            f"  same-class kept {kept:3d}/{len(same)}{marker}"
        )

    # The constant is only defensible while the populations stay separated. If
    # they overlap, no single cutoff is correct and the recall design needs
    # revisiting - say so loudly rather than letting a stale number stand.
    ceiling = max(different) if different else 0.0
    floor = min(same) if same else 1.0
    print(f"\nwrong-class ceiling {ceiling:.3f}   same-class floor {floor:.3f}")
    if ceiling >= floor:
        print(
            f"FAIL: the populations overlap, so no cutoff separates them; "
            f"_SEMANTIC_MATCH_FLOOR={_SEMANTIC_MATCH_FLOOR} cannot be justified from this corpus"
        )
        return 1
    if not ceiling < _SEMANTIC_MATCH_FLOOR < floor:
        print(
            f"FAIL: _SEMANTIC_MATCH_FLOOR={_SEMANTIC_MATCH_FLOOR} is outside the separating gap "
            f"({ceiling:.3f}, {floor:.3f}); pick a value inside it"
        )
        return 1
    if admitted_but_unrelated:
        worst = max(admitted_but_unrelated)
        print(f"FAIL: {len(admitted_but_unrelated)} wrong-class pairs admitted, worst {worst}")
        return 1

    print(
        f"OK: _SEMANTIC_MATCH_FLOOR={_SEMANTIC_MATCH_FLOOR} sits in the gap "
        f"({ceiling:.3f}, {floor:.3f}); {len(different)} wrong-class pairs rejected, "
        f"{len(same)}/{len(same)} same-class pairs kept"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
