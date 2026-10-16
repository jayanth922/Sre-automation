#!/usr/bin/env python3
"""Perturbed copies of review cases, each labelled FAIL by construction.

The real corpus is mostly PASS, so a judge that always says PASS would agree
with the humans. Each copy breaks one thing the rubric checks, in a way the
case itself can disprove:

- swap_cause_effect, drop_link: the chain is reversed, or has a gap between
  two links (causal_chain).
- change_number: a number a tool returned is changed in the claim citing it.
- invent_reference: a claim cites a call this run never made.
- lift_evidence: an entry is taken from a case of another scenario
  (evidence_support for all three).

Only the structured block changes. The narrator's prose (`summary_text`, the
summary event's `content`, `narrative` and `hypothesis`) is left as the run
wrote it, so a judge shown that prose could diff it against the block; the
harness must judge both strata from the same view without it. A copy fails one
criterion; for the other it inherits the source case's adjudicated label.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence, Union

from benchmarks.calibration_cases import CalibrationCaseError, _load_key
from benchmarks.evidence_locator import (
    check_entry,
    tool_calls,
    tool_catalog,
    transcripts_complete,
)

SCHEMA_VERSION = 1

KINDS = {
    "swap_cause_effect": "causal_chain",
    "drop_link": "causal_chain",
    "change_number": "evidence_support",
    "invent_reference": "evidence_support",
    "lift_evidence": "evidence_support",
}

# A standalone number, optionally with a unit. Pod hashes, dates, times,
# versions, IPs, `p99`, `5xx` and `limit=50` are not standalone.
_NUMBER = re.compile(
    r"(?<![\w.:/#=+-])(\d+(?:\.\d+)?)"
    r"(?=(?:%|ms|s|m|h|Mi|Gi|MiB|GiB|KiB|MB|GB|KB)?(?![\w.:/-]?\w))"
)


class PerturbationError(ValueError):
    pass


@dataclass(frozen=True)
class Perturbation:
    kind: str
    evaluation: dict[str, Any]
    detail: dict[str, Any]


@dataclass(frozen=True)
class PerturbedSet:
    review_cases: tuple[dict[str, Any], ...]
    private_mapping: tuple[dict[str, Any], ...]
    skipped: dict[str, dict[str, int]] = field(default_factory=dict)
    key_fingerprint: str = ""


Outcome = Union[Perturbation, str]


def _keyed(key: bytes, material: str) -> int:
    return int(hmac.new(key, material.encode("utf-8"), hashlib.sha256).hexdigest(), 16)


def _pick(key: bytes, material: str, options: Sequence[Any]) -> Any:
    return options[_keyed(key, material) % len(options)]


def _norm(text: Any) -> str:
    return " ".join(str(text or "").split()).casefold()


def _evaluation(case: Mapping[str, Any]) -> dict[str, Any]:
    evaluation = (case.get("review_input") or {}).get("benchmark_evaluation")
    return evaluation if isinstance(evaluation, dict) else {}


def _transcripts(case: Mapping[str, Any]) -> list[dict[str, Any]]:
    transcripts = (case.get("review_input") or {}).get("specialist_transcripts")
    return transcripts if isinstance(transcripts, list) else []


def _complete(case: Mapping[str, Any]) -> bool:
    return bool(_transcripts(case)) and transcripts_complete(case.get("review_input") or {})


def _with(evaluation: Mapping[str, Any], **changes: Any) -> dict[str, Any]:
    out = copy.deepcopy(dict(evaluation))
    out.update(changes)
    return out


def swap_cause_effect(case: Mapping[str, Any], key: bytes) -> Outcome:
    evaluation = _evaluation(case)
    chain = evaluation.get("causal_chain") or []
    candidates = [
        i
        for i, link in enumerate(chain)
        if isinstance(link, dict)
        and _norm(link.get("cause"))
        and _norm(link.get("effect"))
        and _norm(link.get("cause")) != _norm(link.get("effect"))
    ]
    if not candidates:
        return "no link with distinct cause and effect"
    i = _pick(key, f"{case['blind_case_id']}:swap", candidates)
    links = copy.deepcopy(chain)
    links[i]["cause"], links[i]["effect"] = chain[i]["effect"], chain[i]["cause"]
    return Perturbation(
        "swap_cause_effect",
        _with(evaluation, causal_chain=links),
        {"link": i, "cause": chain[i]["cause"], "effect": chain[i]["effect"]},
    )


def drop_link(case: Mapping[str, Any], key: bytes) -> Outcome:
    """Drop an interior link, leaving a gap the chain cannot bridge.

    Dropping the first or last link can leave a shorter chain that still runs
    from an origin to the symptom, so only an interior link whose neighbours
    do not already join is FAIL by construction.
    """
    evaluation = _evaluation(case)
    chain = evaluation.get("causal_chain") or []
    if len(chain) < 3 or not all(isinstance(link, dict) for link in chain):
        return "chain has fewer than 3 links"
    candidates = [
        i
        for i in range(1, len(chain) - 1)
        if _norm(chain[i - 1].get("effect")) != _norm(chain[i + 1].get("cause"))
    ]
    if not candidates:
        return "every interior link's neighbours already join"
    i = _pick(key, f"{case['blind_case_id']}:drop", candidates)
    links = copy.deepcopy(chain[:i] + chain[i + 1:])
    return Perturbation(
        "drop_link",
        _with(evaluation, causal_chain=links),
        {"link": i, "dropped": copy.deepcopy(chain[i])},
    )


def _tool_returns(case: Mapping[str, Any]) -> str:
    return "\n".join(
        str(turn.get("content") or "")
        for transcript in _transcripts(case)
        for turn in transcript.get("turns") or []
        if turn.get("role") == "tool"
    )


def _occurs(token: str, text: str) -> bool:
    return re.search(rf"(?<![\d.]){re.escape(token)}(?!\d|\.\d)", text) is not None


def _scaled(token: str, factor: int) -> str:
    places = len(token.partition(".")[2])
    return f"{float(token) * factor:.{places}f}"


def change_number(case: Mapping[str, Any], key: bytes) -> Outcome:
    """Change a number in a claim that a tool return states verbatim.

    The original stays in the transcript, so the claim is disprovable from the
    case itself. A number no return states (a percentage computed from a
    ratio, say) is never chosen, since changing it proves nothing.
    """
    evaluation = _evaluation(case)
    evidence = evaluation.get("evidence") or []
    returns = _tool_returns(case)
    if not returns:
        return "no tool returns attached"
    candidates = []
    for i, entry in enumerate(evidence):
        claim = entry.get("claim") if isinstance(entry, dict) else None
        if not isinstance(claim, str):
            continue
        for token in dict.fromkeys(m.group(1) for m in _NUMBER.finditer(claim)):
            if "." not in token and 1900 <= int(token) <= 2100:
                continue
            if _occurs(token, returns):
                candidates.append((i, token))
    if not candidates:
        return "no claimed number appears in a tool return"
    # A small integer occurs all over a JSON return, so finding it there says
    # little about whether the return states this claim's value.
    distinctive = [(i, t) for i, t in candidates if "." in t or int(t) >= 10]
    i, token = _pick(key, f"{case['blind_case_id']}:number", distinctive or candidates)
    for factor in (3, 5, 7):
        new = _scaled(token, factor)
        if new != token and not _occurs(new, returns):
            break
    else:
        return f"no replacement for {token} is absent from the tool returns"
    entries = copy.deepcopy(evidence)
    claim = entries[i]["claim"]
    entries[i]["claim"] = _NUMBER.sub(
        lambda m: new if m.group(1) == token else m.group(0), claim
    )
    return Perturbation(
        "change_number",
        _with(evaluation, evidence=entries),
        {"evidence": i, "from": token, "to": new, "claim": claim},
    )


def _foreign_entries(
    case: Mapping[str, Any],
    donors: Sequence[Mapping[str, Any]],
    scenarios: Mapping[str, str],
    catalog: frozenset[str],
    *,
    unlocatable_only: bool,
) -> list[tuple[str, int, dict[str, Any], str]]:
    """Evidence entries of other scenarios' cases that this case cannot locate."""
    own = scenarios.get(case["blind_case_id"])
    calls = tool_calls(_transcripts(case))
    complete = _complete(case)
    claims = {_norm(e.get("claim")) for e in _evaluation(case).get("evidence") or [] if isinstance(e, dict)}
    found = []
    for donor in donors:
        donor_id = donor["blind_case_id"]
        if donor_id == case["blind_case_id"] or scenarios.get(donor_id) in (None, own):
            continue
        for j, entry in enumerate(_evaluation(donor).get("evidence") or []):
            if not isinstance(entry, dict) or _norm(entry.get("claim")) in claims:
                continue
            state = check_entry(
                0, entry, calls, transcripts_complete=complete, catalog=catalog
            ).state
            if state == "UNLOCATABLE" or (state == "UNCHECKABLE" and not unlocatable_only):
                found.append((donor_id, j, entry, state))
    return found


def invent_reference(
    case: Mapping[str, Any],
    key: bytes,
    donors: Sequence[Mapping[str, Any]],
    scenarios: Mapping[str, str],
    catalog: frozenset[str],
) -> Outcome:
    """Cite, for one of this case's claims, a real call from another run.

    The donor's reference is a call this run provably never made, so the entry
    is unlocatable while its claim stays the case's own.
    """
    evidence = _evaluation(case).get("evidence") or []
    if not evidence:
        return "no evidence entries"
    if not _complete(case):
        return "transcripts missing, so no call is provably absent"
    foreign = _foreign_entries(case, donors, scenarios, catalog, unlocatable_only=True)
    if not foreign:
        return "no other scenario's reference is unlocatable here"
    i = _pick(key, f"{case['blind_case_id']}:invent:entry", range(len(evidence)))
    entries = copy.deepcopy(evidence)
    original = entries[i].get("reference")
    foreign = [f for f in foreign if _norm(f[2]["reference"]) != _norm(original)]
    if not foreign:
        return "no other scenario's reference differs from the chosen entry's"
    donor_id, j, donor_entry, _ = _pick(key, f"{case['blind_case_id']}:invent:donor", foreign)
    entries[i]["reference"] = donor_entry["reference"]
    return Perturbation(
        "invent_reference",
        _with(_evaluation(case), evidence=entries),
        {"evidence": i, "from": original, "to": donor_entry["reference"], "donor": donor_id, "donor_evidence": j},
    )


def lift_evidence(
    case: Mapping[str, Any],
    key: bytes,
    donors: Sequence[Mapping[str, Any]],
    scenarios: Mapping[str, str],
    catalog: frozenset[str],
) -> Outcome:
    """Replace one entry with another scenario's entry this case cannot locate."""
    evidence = _evaluation(case).get("evidence") or []
    if not evidence:
        return "no evidence entries"
    if not _transcripts(case):
        return "no transcripts attached, so the lifted claim cannot be disproved"
    foreign = _foreign_entries(case, donors, scenarios, catalog, unlocatable_only=False)
    if not foreign:
        return "no other scenario's entry is unlocated here"
    i = _pick(key, f"{case['blind_case_id']}:lift:entry", range(len(evidence)))
    donor_id, j, donor_entry, state = _pick(key, f"{case['blind_case_id']}:lift:donor", foreign)
    entries = copy.deepcopy(evidence)
    replaced = entries[i]
    entries[i] = copy.deepcopy(donor_entry)
    return Perturbation(
        "lift_evidence",
        _with(_evaluation(case), evidence=entries),
        {
            "evidence": i,
            "replaced": replaced,
            "donor": donor_id,
            "donor_evidence": j,
            # UNCHECKABLE: a prose reference, so only the judge can disprove it.
            "donor_reference_state": state,
        },
    )


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def perturbed_case(
    case: Mapping[str, Any], perturbation: Perturbation, blind_case_id: str
) -> dict[str, Any]:
    """The source case with the block replaced everywhere the run copied it.

    The summary event carries the same block the review input does; a copy
    left unperturbed there would hand the judge the original.
    """
    out = copy.deepcopy(dict(case))
    original = _canonical(_evaluation(case))
    review_input = out["review_input"]
    review_input["benchmark_evaluation"] = copy.deepcopy(perturbation.evaluation)
    for event in review_input.get("timeline_events") or []:
        payload = event.get("payload") if isinstance(event, dict) else None
        if (
            isinstance(payload, dict)
            and event.get("event_type") == "summary"
            and _canonical(payload.get("benchmark_evaluation")) == original
        ):
            payload["benchmark_evaluation"] = copy.deepcopy(perturbation.evaluation)
    out["blind_case_id"] = blind_case_id
    return out


def build_perturbed_set(
    cases: Sequence[Mapping[str, Any]],
    mapping: Sequence[Mapping[str, Any]],
    *,
    blind_key: bytes,
    catalog: Optional[frozenset[str]] = None,
    per_kind: Optional[int] = None,
) -> PerturbedSet:
    """One copy per applicable (case, kind); at most `per_kind` of each kind."""
    if len(blind_key) < 32:
        raise PerturbationError("blind key must contain at least 32 bytes")
    if per_kind is not None and per_kind < 1:
        raise PerturbationError("per_kind must be positive")
    if catalog is None:
        catalog = tool_catalog()
    scenarios = {row["blind_case_id"]: row.get("scenario") for row in mapping}
    unmapped = [c["blind_case_id"] for c in cases if c["blind_case_id"] not in scenarios]
    if unmapped:
        raise PerturbationError(f"{len(unmapped)} cases are not in the private mapping")
    real_ids = {c["blind_case_id"] for c in cases}

    makers: dict[str, Callable[[Mapping[str, Any]], Outcome]] = {
        "swap_cause_effect": lambda c: swap_cause_effect(c, blind_key),
        "drop_link": lambda c: drop_link(c, blind_key),
        "change_number": lambda c: change_number(c, blind_key),
        "invent_reference": lambda c: invent_reference(c, blind_key, cases, scenarios, catalog),
        "lift_evidence": lambda c: lift_evidence(c, blind_key, cases, scenarios, catalog),
    }
    made: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    skipped: dict[str, dict[str, int]] = {}
    for kind, make in makers.items():
        produced = []
        for case in cases:
            outcome = make(case)
            if isinstance(outcome, str):
                reasons = skipped.setdefault(kind, {})
                reasons[outcome] = reasons.get(outcome, 0) + 1
                continue
            digest = hmac.new(
                blind_key,
                f"perturbed:{case['blind_case_id']}:{kind}".encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            blind_case_id = f"case-{digest[:24]}"
            if blind_case_id in real_ids:
                raise PerturbationError(f"{blind_case_id} collides with a real case")
            produced.append(
                (
                    blind_case_id,
                    perturbed_case(case, outcome, blind_case_id),
                    {
                        "blind_case_id": blind_case_id,
                        "source_blind_case_id": case["blind_case_id"],
                        "scenario": scenarios[case["blind_case_id"]],
                        "kind": kind,
                        "criterion": KINDS[kind],
                        "label": "FAIL",
                        "detail": outcome.detail,
                    },
                )
            )
        produced.sort(key=lambda item: item[0])
        if per_kind is not None:
            produced = produced[:per_kind]
        made.extend(produced)
    made.sort(key=lambda item: item[0])
    return PerturbedSet(
        review_cases=tuple(case for _, case, _ in made),
        private_mapping=tuple(row for _, _, row in made),
        skipped=skipped,
        key_fingerprint=hashlib.sha256(blind_key).hexdigest(),
    )


def _jsonl(rows: Sequence[Mapping[str, Any]]) -> bytes:
    return b"".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        for row in rows
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def write_perturbed_set(
    perturbed: PerturbedSet,
    *,
    source_review: bytes,
    source_mapping: bytes,
    review_path: Path,
    mapping_path: Path,
    manifest_path: Path,
) -> None:
    review = _jsonl(perturbed.review_cases)
    mapping = _jsonl(perturbed.private_mapping)
    counts: dict[str, int] = {kind: 0 for kind in KINDS}
    for row in perturbed.private_mapping:
        counts[row["kind"]] += 1
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "cases": len(perturbed.review_cases),
        "kinds": counts,
        "skipped": perturbed.skipped,
        "source_review_sha256": hashlib.sha256(source_review).hexdigest(),
        "source_private_mapping_sha256": hashlib.sha256(source_mapping).hexdigest(),
        "blind_key_fingerprint": perturbed.key_fingerprint,
        "review_sha256": hashlib.sha256(review).hexdigest(),
        "private_mapping_sha256": hashlib.sha256(mapping).hexdigest(),
    }
    for path in (review_path, mapping_path, manifest_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_bytes(review)
    # The mapping says which cases are perturbed and how; it is as private as
    # the blind key.
    descriptor = os.open(mapping_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(mapping)
    os.chmod(mapping_path, 0o600)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("review", type=Path, help="review.jsonl from calibration_cases")
    parser.add_argument("--private-mapping", type=Path, required=True)
    parser.add_argument("--blind-key-file", type=Path, required=True)
    parser.add_argument("--review-output", type=Path, required=True)
    parser.add_argument("--private-mapping-output", type=Path, required=True)
    parser.add_argument("--manifest-output", type=Path, required=True)
    parser.add_argument("--per-kind", type=int)
    args = parser.parse_args(argv)
    try:
        source_review = args.review.read_bytes()
        source_mapping = args.private_mapping.read_bytes()
        catalog = tool_catalog()
        if not catalog:
            raise PerturbationError("no MCP tool catalog to check references against")
        perturbed = build_perturbed_set(
            _read_jsonl(args.review),
            _read_jsonl(args.private_mapping),
            blind_key=_load_key(args.blind_key_file),
            catalog=catalog,
            per_kind=args.per_kind,
        )
        write_perturbed_set(
            perturbed,
            source_review=source_review,
            source_mapping=source_mapping,
            review_path=args.review_output,
            mapping_path=args.private_mapping_output,
            manifest_path=args.manifest_output,
        )
    except (OSError, CalibrationCaseError, PerturbationError) as exc:
        parser.error(str(exc))
    print(f"perturbed cases: {len(perturbed.review_cases)}")
    for kind in KINDS:
        made = sum(1 for row in perturbed.private_mapping if row["kind"] == kind)
        print(f"  {kind}: {made}")
        for reason, count in sorted(perturbed.skipped.get(kind, {}).items()):
            print(f"    skipped {count}: {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
