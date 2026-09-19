#!/usr/bin/env python3
"""Can the `no_memory` arm observe anything on this split?

The arm removes two lookups: verified-skill proposal and incident recall. What
it cannot remove is knowledge that was never there. Learned-memory writes are
frozen for every arm during an experiment — deliberately, so arms that run
sequentially cannot hand each other a corpus — which means whatever `full`
retrieves has to pre-exist the run. If the corpus holds nothing matching a
scenario, `full` and `no_memory` execute the same lookups, get the same
nothing, and behave identically on that pair.

That failure is invisible in the results. It does not error, it does not warn;
it reports `NOT_DEMONSTRATED`, which reads exactly like the considered finding
"learned memory does not earn its complexity". The two are opposite claims and
the artifact cannot tell them apart. Hence a preflight: measure coverage
*before* spending an arm's API budget, and record the number so a null result
can be read correctly afterwards.

Two checks, and they differ in strength:

* **Verified skills — exact.** Replays `propose_skills`, the same call the
  planner makes, against the same store. What this reports is what the arm
  would retrieve.
* **Incident recall — necessary, not sufficient.** Counts tenant-scoped points
  in the incident collection rather than running a semantic query, which would
  need the embedding model and an API bill of its own. Zero points proves
  recall cannot fire. A non-zero count proves only that it *might*.

A pair is *blind* when neither lookup can return anything. Blind pairs carry no
information about learned memory, whatever the outcome.

**Run this where the agent runs.** `SemanticSkillStore` degrades to keyword-only
recall whenever `qdrant-client` or the embedding model is unavailable, and it
does so with a log line rather than an error. An operator host with the dev
dependencies installed therefore answers a different question than the agent
container without them — measured here, the same corpus and the same six
scenarios gave 6/6 coverage on the host and 3/6 inside the container. The
report records which path it took so a coverage number can never be read
without it; a number measured on a path the agent does not use is worse than
no number, because it reassures.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scenario_dataset import load_dataset  # noqa: E402

EXIT_OK = 0
EXIT_BLIND = 1
EXIT_ERROR = 2


def alert_probe(scenario: Any) -> dict[str, Any]:
    """The alert shaped the way `signature_from_alert` reads it at runtime.

    The dataset stores `alert.alertname` and `alert.service` flat; the runtime
    signature reads `alert_name` and `labels.service`. Getting this mapping
    wrong silently yields failure class `unknown`, which matches no skill and
    would make this preflight report a corpus gap that is really a probe bug.
    """
    alert = getattr(scenario, "alert", None) or {}
    return {
        "alert_name": alert.get("alertname"),
        "labels": {"service": alert.get("service") or "unknown"},
    }


def retrieval_path(store: Any) -> str:
    """Which recall path this process actually has.

    `SemanticSkillStore` degrades to keyword-only when `qdrant-client` is
    missing, when Qdrant is unreachable, or when the embedding model will not
    load — each with a log line and no error. The coverage number differs by
    path: on this corpus, semantic recall covered 6/6 scenarios and
    keyword-only 3/6. A coverage report that does not say which path produced
    it cannot be acted on.
    """
    return "semantic" if getattr(store, "_semantic_available", False) else "keyword_only"


def open_store_read_only() -> Any:
    """The agent's own store, with its one constructor write suppressed.

    `SemanticSkillStore.__init__` backfills the Qdrant index. That is right for
    a long-lived agent and wrong for a preflight, which would then be reporting
    on a corpus it had itself just modified — observed here as a
    `PUT /collections/sre_skills_v1/points` during what was supposed to be a
    measurement.

    Patching a private name is deliberate, and it fails closed: if the method
    is renamed, this raises rather than quietly resuming the write. `add()` is
    the only other index write and the preflight never calls it.
    """
    from sre_agent import skill_store as module

    cls = module.SemanticSkillStore
    if not hasattr(cls, "_backfill_index"):
        raise RuntimeError(
            "SemanticSkillStore._backfill_index is gone. This preflight suppressed "
            "it to stay read-only and cannot assume the constructor stopped writing."
        )
    original = cls._backfill_index
    cls._backfill_index = lambda self: None
    try:
        return module.get_skill_store()
    finally:
        cls._backfill_index = original


def assess_split(
    scenarios: Sequence[Any],
    *,
    propose: Callable[[dict[str, Any]], Sequence[Any]],
    signature_of: Callable[[dict[str, Any]], Any],
    incident_points: Optional[int],
    path: str,
) -> dict[str, Any]:
    """Per-scenario coverage plus the totals a verdict is read from.

    `incident_points` is None when the collection could not be reached — which
    is not the same as empty, and is reported as unknown rather than folded
    into either answer.

    `path` is required rather than derived, because every count below is only
    meaningful relative to the retrieval path that produced it.
    """
    recall_possible = bool(incident_points)
    pairs: list[dict[str, Any]] = []
    for scenario in scenarios:
        probe = alert_probe(scenario)
        signature = signature_of(probe)
        skills = list(propose(probe))
        pairs.append(
            {
                "scenario": getattr(scenario, "name", "?"),
                "alert_name": probe["alert_name"],
                "service": probe["labels"]["service"],
                "failure_class": getattr(signature, "failure_class", "unknown"),
                "skills_retrieved": [
                    getattr(s, "skill_id", "?") for s in skills
                ],
                "skill_hit": bool(skills),
                # Per-scenario only in the sense that recall is reachable at
                # all; a point count cannot say which scenario would match.
                "recall_possible": recall_possible,
                "blind": not skills and not recall_possible,
            }
        )
    blind = [p for p in pairs if p["blind"]]
    with_skills = [p for p in pairs if p["skill_hit"]]
    return {
        "scenario_count": len(pairs),
        "skill_hits": len(with_skills),
        "incident_memory_points": incident_points,
        "recall_possible": recall_possible,
        "retrieval_path": path,
        "blind_pairs": [p["scenario"] for p in blind],
        "observable_pairs": len(pairs) - len(blind),
        "pairs": pairs,
    }


def verdict_lines(report: dict[str, Any]) -> list[str]:
    """What the operator needs to decide whether to spend the arm's budget."""
    total = report["scenario_count"]
    observable = report["observable_pairs"]
    lines = []
    if observable == 0:
        lines.append(
            f"BLIND: none of {total} scenarios can retrieve learned memory. "
            "The no_memory arm would remove nothing, and its NOT_DEMONSTRATED "
            "verdict would be an artifact of the empty corpus, not a finding."
        )
    elif observable < total:
        lines.append(
            f"PARTIAL: {observable}/{total} scenarios can retrieve learned "
            f"memory. The other {total - observable} carry no information "
            "about it and dilute the paired delta toward zero."
        )
    else:
        lines.append(f"COVERED: all {total} scenarios can retrieve learned memory.")
    if report["incident_memory_points"] == 0:
        lines.append(
            "Incident recall is provably inert: the collection holds no "
            "tenant-scoped points, so half of what the arm removes is already "
            "absent from every arm."
        )
    elif report["incident_memory_points"] is None:
        lines.append(
            "Incident collection unreachable; recall coverage is unknown and "
            "was not counted toward observability."
        )
    path = report.get("retrieval_path")
    if path == "keyword_only":
        lines.append(
            "Retrieval path: keyword_only — semantic skill recall is OFF in this "
            "process (qdrant-client missing, Qdrant unreachable, or the embedding "
            "model failed to load). The count above is the keyword-only count."
        )
    else:
        lines.append(f"Retrieval path: {path}.")
    lines.append(
        "Valid only if the agent's own process takes that same path. Confirm it "
        "there, not here: the paths disagreed 6/6 against 3/6 on this corpus."
    )
    return lines


def count_incident_points(
    url: str, collection: str, org: Optional[str], cluster: Optional[str]
) -> Optional[int]:
    """Tenant-scoped point count, or None when the store cannot be reached."""
    import urllib.error
    import urllib.request

    must: list[dict[str, Any]] = []
    if org:
        must.append({"key": "organization_id", "match": {"value": org}})
    if cluster:
        must.append({"key": "cluster_id", "match": {"value": cluster}})
    body = json.dumps({"filter": {"must": must}, "exact": True}).encode()
    request = urllib.request.Request(
        f"{url.rstrip('/')}/collections/{collection}/points/count",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return int(json.load(response)["result"]["count"])
    except (urllib.error.URLError, KeyError, ValueError, OSError):
        return None


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", default=os.getenv("BENCH_DATASET_ROOT"))
    parser.add_argument("--version", default=os.getenv("BENCH_DATASET_VERSION", "v2"))
    parser.add_argument("--split", default=os.getenv("BENCH_DATASET_SPLIT", "dev"))
    parser.add_argument("--organization-id", default=os.getenv("BENCH_ORGANIZATION_ID"))
    parser.add_argument("--cluster-id", default=os.getenv("BENCH_CLUSTER_ID"))
    parser.add_argument("--qdrant-url", default=os.getenv("QDRANT_URL", "http://localhost:6333"))
    parser.add_argument(
        "--expect-retrieval-path",
        choices=("semantic", "keyword_only"),
        help=(
            "fail if this process does not take the named recall path. Pass the "
            "path the agent itself takes, so a preflight run in the wrong "
            "environment errors instead of reporting a number for a stack "
            "nothing runs."
        ),
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(list(argv) if argv is not None else None)

    root = (
        Path(args.dataset_root)
        if args.dataset_root
        else Path(__file__).resolve().parent / "datasets"
    )
    try:
        dataset = load_dataset(root, args.version, args.split)
    except Exception as exc:  # dataset errors are operator errors, not findings
        print(f"error: could not load dataset: {exc}", file=sys.stderr)
        return EXIT_ERROR

    from sre_agent.memory_store import INCIDENTS_COLLECTION
    from sre_agent.skill_store import propose_skills, signature_from_alert

    try:
        store = open_store_read_only()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    path = retrieval_path(store)
    if args.expect_retrieval_path and path != args.expect_retrieval_path:
        print(
            f"error: retrieval path is {path}, expected "
            f"{args.expect_retrieval_path}. This process is not the one the "
            "agent runs, so its coverage number does not transfer.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    org, cluster = args.organization_id, args.cluster_id
    report = assess_split(
        dataset.scenarios,
        propose=lambda probe: propose_skills(
            store, probe, organization_id=org, cluster_id=cluster
        ),
        signature_of=lambda probe: signature_from_alert(
            probe, organization_id=org, cluster_id=cluster
        ),
        incident_points=count_incident_points(
            args.qdrant_url, INCIDENTS_COLLECTION, org, cluster
        ),
        path=path,
    )
    report.update(
        {
            "dataset_version": dataset.dataset_version,
            "dataset_split": dataset.split,
            "dataset_sha256": dataset.sha256,
            "organization_id": org,
            "cluster_id": cluster,
            "skills_in_store": len(store.all()),
        }
    )

    for pair in report["pairs"]:
        mark = "blind" if pair["blind"] else "ok   "
        print(
            f"{mark} {pair['scenario']:36} {pair['failure_class']:16}"
            f"{pair['service']:20} skills={len(pair['skills_retrieved'])}"
        )
    print()
    for line in verdict_lines(report):
        print(line)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True), "utf-8")
        print(f"\nwrote {args.output}")

    return EXIT_OK if report["observable_pairs"] else EXIT_BLIND


if __name__ == "__main__":
    raise SystemExit(main())
