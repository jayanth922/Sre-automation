# Structured benchmark graders

`v1/rubric.json` pins the criteria and grading method used for A04. Deterministic
criteria consume typed fields only; free-text keyword matches are not accepted.
Causal-chain and evidence-support judgments remain
`REQUIRES_CALIBRATION` until a blinded, human-labeled calibration set exists.

`v1/calibration-label.schema.json` defines that label contract. No calibration
labels are checked into the repository yet, so the evaluator must not report
judge agreement or semantic-grade accuracy. Future labels must use opaque case
IDs, the same two independent labelers for every case, and adjudication for
disagreement before any model judge can become release-authoritative.

Build the blinded review set from raw grader evidence without exposing scenario
IDs or ground truth to either labeler:

```bash
OUT=reports/judge-calibration; mkdir -p "$OUT"
# Tool returns, so evidence claims can be checked: verified, content-addressed.
docker exec -i sre-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -F "|"' <<'SQL' \
  | PYTHONPATH=evals:src .venv/bin/python -m benchmarks.transcript_store "$OUT/transcripts"
select content_sha256, replace(encode(payload, 'base64'), chr(10), '')
from evidence_artifacts where kind = 'specialist_tool_trace';
SQL
openssl rand 32 > "$OUT/blind.key"
PYTHONPATH=evals .venv/bin/python -m benchmarks.calibration_cases \
  $(find reports -name '*grades*.jsonl' -not -path "$OUT/*" | sort) \
  --blind-key-file "$OUT/blind.key" \
  --transcript-dir "$OUT/transcripts" \
  --review-output "$OUT/review.jsonl" \
  --private-mapping-output "$OUT/private-map.jsonl" \
  --manifest-output "$OUT/manifest.json"
# The Codespace's default ACL overrides umask; set the modes explicitly.
chmod 600 "$OUT/blind.key" "$OUT/private-map.jsonl"
```

Several grader files may be given; a run copied into more than one report
directory becomes one case. With `--transcript-dir`, each case carries the
specialist tool calls and returns its findings reference (thinking blocks
dropped), and the manifest counts attached, missing, and cases that reference
none. See `CALIBRATION_DESIGN.md` for the rubric and agreement plan.

The HMAC key makes selection reproducible without putting scenario identity in
the review artifact. The private map and key stay with the evaluation owner;
labelers receive only `calibration-review.jsonl`. The script rejects duplicate
outputs and digest mismatches — those say the evidence file is not what it
claims to be, and the build stops. A record carrying no structured evaluation,
or graded against a rubric that has since changed, is skipped instead: it is
one unreviewable run, not a corrupt corpus. Every skip is counted by reason in
the manifest (`skipped_records`, `skipped_reasons`) and printed, because
silently dropping cases would bias the set toward whatever the current rubric
happens to grade. A file with nothing reviewable in it still fails closed. Source-output hashes remain only in the
private map, so a labeler cannot join an opaque case back to the raw corpus. It
never calls a model or Langfuse, so creating the review set has no API cost.

When labels exist, measure and gate agreement with:

```bash
python evals/benchmarks/grader_calibration.py labels.jsonl \
  --minimum-cases 20 --minimum-kappa 0.6
```

The command fails when cases are under-labeled, labels lack class variation,
or either semantic criterion misses the configured Cohen's kappa threshold.
