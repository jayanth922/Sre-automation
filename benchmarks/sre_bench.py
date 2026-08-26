#!/usr/bin/env python3
"""
SRE-Agent Benchmark — independently verified recovery and quality.

Extends the MTTR benchmark into a full domain benchmark for the multi-agent SRE
system. For each fault scenario (with known ground truth) it fires a synthetic
alert, observes a scenario-owned Prometheus recovery probe outside Sentinel,
fetches the incident transcript, and scores:

    verified recovery · MTTR · root-cause · remediation · severity · safety

across `RUNS_PER_SCENARIO` repeats (pass^k consistency), then prints a
leaderboard-style report.

Prerequisites (runs against the LIVE platform, not in CI):
- Platform + edge up (`./main_start.sh`) and a client environment connected
  (e.g. the reference client `../meridian-shop`).
- For the remediation/severity/safety columns, enable the ACT phase:
  `ACT_PHASE_ENABLED=true` (and optionally `EXECUTOR_LIVE=true`).

Run:
    uv run python benchmarks/sre_bench.py

Config via env (falls back to the bench_mttr defaults):
    BENCH_BASE_URL, BENCH_ADMIN_EMAIL, BENCH_ADMIN_PASSWORD,
    BENCH_CLUSTER_ID, BENCH_CLUSTER_TOKEN, BENCH_RUNS_PER_SCENARIO,
    BENCH_PROMETHEUS_URL, BENCH_ORACLE_RESULTS_PATH
"""

import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fault_adapter import MeridianAdminConfigAdapter  # noqa: E402
from recovery_oracle import (  # noqa: E402
    PrometheusOracleClient,
    RecoveryOracleTracker,
    append_oracle_result,
)
from scenario_dataset import load_dataset  # noqa: E402
from scoring import ScenarioSpec, aggregate, score_run  # noqa: E402
from statistical_eval import (  # noqa: E402
    append_trial,
    build_trial_record,
    build_trial_schedule,
    make_pair_id,
)
from structured_grading import append_grader_record  # noqa: E402

# ── Config ──────────────────────────────────────────────────────────────────
BASE_URL = os.getenv("BENCH_BASE_URL", "http://localhost:8080")
ADMIN_EMAIL = os.getenv("BENCH_ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.getenv("BENCH_ADMIN_PASSWORD", "admin")
CLUSTER_ID = os.getenv("BENCH_CLUSTER_ID", "df4ab154-2b84-4570-93c6-9c9a70ef9baf")
CLUSTER_TOKEN = os.getenv("BENCH_CLUSTER_TOKEN", "cl_438450df3cb94ea78760f4e005088c2a")
RUNS_PER_SCENARIO = int(os.getenv("BENCH_RUNS_PER_SCENARIO", "3"))
PROMETHEUS_URL = os.getenv("BENCH_PROMETHEUS_URL", "http://localhost:9090")
PROMETHEUS_TOKEN = os.getenv("BENCH_PROMETHEUS_BEARER_TOKEN")
ORACLE_RESULTS_PATH = Path(
    os.getenv("BENCH_ORACLE_RESULTS_PATH", "reports/sre-bench-oracle.jsonl")
)
GRADER_RESULTS_PATH = Path(
    os.getenv("BENCH_GRADER_RESULTS_PATH", "reports/sre-bench-grades.jsonl")
)
TRIAL_RESULTS_PATH = Path(
    os.getenv("BENCH_TRIAL_RESULTS_PATH", "reports/sre-bench-trials.jsonl")
)
EXPERIMENT_ID = os.getenv("BENCH_EXPERIMENT_ID", "").strip()
CANDIDATE_ID = os.getenv("BENCH_CANDIDATE_ID", "").strip()
CONFIG_FINGERPRINT = os.getenv("BENCH_CONFIG_FINGERPRINT", "").strip()
PAIR_SEED = os.getenv("BENCH_PAIR_SEED", "").strip()
STATISTICAL_CONFIG = {
    "experiment_id": EXPERIMENT_ID,
    "candidate_id": CANDIDATE_ID,
    "config_fingerprint": CONFIG_FINGERPRINT,
    "pair_seed": PAIR_SEED,
}
STATISTICAL_RECORDING = any(STATISTICAL_CONFIG.values())
if STATISTICAL_RECORDING and not all(STATISTICAL_CONFIG.values()):
    missing = sorted(key for key, value in STATISTICAL_CONFIG.items() if not value)
    raise RuntimeError(
        f"statistical recording requires all BENCH experiment fields; missing={missing}"
    )
DATASET_ROOT = Path(
    os.getenv(
        "BENCH_DATASET_ROOT",
        str(Path(__file__).resolve().parent / "datasets"),
    )
)
DATASET_VERSION = os.getenv("BENCH_DATASET_VERSION", "v1")
DATASET_SPLIT = os.getenv("BENCH_DATASET_SPLIT", "dev")
ALLOW_HOLDOUT = os.getenv("BENCH_ALLOW_HOLDOUT", "").lower() in {
    "1",
    "true",
    "yes",
}
FAULT_MODE = os.getenv("BENCH_FAULT_MODE", "none").strip().lower()
if FAULT_MODE not in {"none", "manual", "automatic"}:
    raise RuntimeError("BENCH_FAULT_MODE must be none, manual, or automatic")
FAULT_SERVICE_URLS = {
    "checkout-service": os.getenv("BENCH_CHECKOUT_URL", "http://localhost:8001"),
    "inventory-service": os.getenv("BENCH_INVENTORY_URL", "http://localhost:8002"),
    "payment-service": os.getenv("BENCH_PAYMENT_URL", "http://localhost:8004"),
}
POLL_INTERVAL_SEC = 5
TIMEOUT_SEC = 300
COOLDOWN_SEC = 30
ORACLE_COMPLETION_GRACE_SEC = int(os.getenv("BENCH_ORACLE_COMPLETION_GRACE_SEC", "30"))

TERMINAL_APPLICATION_STATUSES = {
    "investigated",
    "awaiting_approval",
    "remediation_failed",
    "verification_unknown",
    "resolved",
}

DATASET = load_dataset(
    DATASET_ROOT,
    DATASET_VERSION,
    DATASET_SPLIT,
    allow_holdout=ALLOW_HOLDOUT,
)
SCENARIOS = DATASET.scenarios


async def _login(client: httpx.AsyncClient) -> str:
    r = await client.post(
        f"{BASE_URL}/auth/token",
        data={"username": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    r.raise_for_status()
    return r.json()["access_token"]


async def _incident_ids(client: httpx.AsyncClient, jwt: str) -> set[str]:
    r = await client.get(
        f"{BASE_URL}/api/v1/clusters/{CLUSTER_ID}/incidents",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    r.raise_for_status()
    return {inc["id"] for inc in r.json()}


async def _fire_alert(
    client: httpx.AsyncClient, spec: ScenarioSpec, started_at: datetime
) -> None:
    payload = {
        "version": "4",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": spec.alert["alertname"],
                    "severity": spec.alert["severity"],
                    "service": spec.alert["service"],
                },
                "annotations": {
                    "summary": spec.alert["summary"],
                    "description": spec.alert["description"],
                },
                "startsAt": started_at.isoformat(),
            }
        ],
    }
    r = await client.post(
        f"{BASE_URL}/api/v1/alerts/webhook",
        json=payload,
        headers={"Authorization": f"Bearer {CLUSTER_TOKEN}"},
    )
    r.raise_for_status()


async def _wait_new_incident(client, jwt, known) -> Optional[dict]:
    for _ in range(10):
        await asyncio.sleep(2)
        r = await client.get(
            f"{BASE_URL}/api/v1/clusters/{CLUSTER_ID}/incidents",
            headers={"Authorization": f"Bearer {jwt}"},
        )
        r.raise_for_status()
        for inc in r.json():
            if inc["id"] not in known:
                return inc
    return None


async def _fetch_incident(client, jwt, incident_id) -> Optional[dict]:
    response = await client.get(
        f"{BASE_URL}/api/v1/clusters/{CLUSTER_ID}/incidents",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    response.raise_for_status()
    return next(
        (incident for incident in response.json() if incident["id"] == incident_id),
        None,
    )


async def _observe_oracle(
    client: httpx.AsyncClient,
    oracle_client: PrometheusOracleClient,
    tracker: RecoveryOracleTracker,
    *,
    baseline: bool = False,
) -> None:
    try:
        value = await oracle_client.query(client, tracker.probe)
    except Exception as exc:
        if baseline:
            tracker.establish_baseline(None, error=f"{type(exc).__name__}: {exc}")
        else:
            tracker.observe(None, error=f"{type(exc).__name__}: {exc}")
    else:
        if baseline:
            tracker.establish_baseline(value)
        else:
            tracker.observe(value)


async def _await_manual_fault(
    spec: ScenarioSpec, tracker: RecoveryOracleTracker
) -> None:
    if FAULT_MODE != "manual" or tracker.baseline_healthy is not True:
        return
    target = spec.fault["target"]
    inject = spec.fault["inject"]["payload"]
    cleanup = spec.fault["cleanup"]["payload"]
    prompt = (
        f"\nBaseline is healthy for {spec.name}. Apply {inject} to {target}, "
        f"then press Enter. Required cleanup: {cleanup}. "
    )
    await asyncio.to_thread(input, prompt)


async def _await_manual_cleanup(spec: ScenarioSpec) -> None:
    if FAULT_MODE != "manual":
        return
    target = spec.fault["target"]
    cleanup = spec.fault["cleanup"]["payload"]
    await asyncio.to_thread(
        input,
        f"\nRestore {target} to {cleanup}, verify it, then press Enter. ",
    )


async def _wait_for_recovery(
    client: httpx.AsyncClient,
    jwt: str,
    incident: dict,
    oracle_client: PrometheusOracleClient,
    tracker: RecoveryOracleTracker,
) -> dict:
    """Poll independent evidence; application status is context, never the oracle."""
    elapsed = 0
    terminal_seen_at: Optional[int] = None
    latest = incident

    while elapsed < TIMEOUT_SEC:
        await asyncio.sleep(POLL_INTERVAL_SEC)
        elapsed += POLL_INTERVAL_SEC
        await _observe_oracle(client, oracle_client, tracker)

        current = await _fetch_incident(client, jwt, incident["id"])
        if current is not None:
            latest = current
        application_status = str(latest.get("status") or "").lower()

        if tracker.recovered_at is not None:
            break
        if application_status in TERMINAL_APPLICATION_STATUSES:
            if terminal_seen_at is None:
                terminal_seen_at = elapsed
            elif elapsed - terminal_seen_at >= ORACLE_COMPLETION_GRACE_SEC:
                break
        else:
            terminal_seen_at = None

    return latest


async def _fetch_transcript(client, jwt, incident_id) -> dict:
    r = await client.get(
        f"{BASE_URL}/api/v1/incidents/{incident_id}/transcript",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    r.raise_for_status()
    return r.json()


def _oracle_result(
    tracker: RecoveryOracleTracker,
    spec: ScenarioSpec,
    *,
    incident_id: Optional[str],
    application_status: str,
):
    return tracker.result(
        scenario=spec.name,
        incident_id=incident_id,
        application_status=application_status,
        dataset_version=spec.dataset_version,
        scenario_version=spec.scenario_version,
        dataset_split=DATASET.split,
        dataset_sha256=DATASET.sha256,
    )


def _score_without_output(spec: ScenarioSpec, result):
    return score_run(
        spec,
        result.status,
        result.application_status,
        "",
        [],
    )


def _record_grade(
    spec: ScenarioSpec,
    result,
    summary_text: str,
    events: list[dict],
    score,
) -> None:
    append_grader_record(
        GRADER_RESULTS_PATH,
        spec=spec,
        oracle_status=result.status,
        application_status=result.application_status,
        summary_text=summary_text,
        events=events,
        score=score,
    )


def _failure_categories(score) -> tuple[str, ...]:
    categories: set[str] = set()
    if score.oracle_status == "INVALID_SCENARIO":
        categories.add("invalid_scenario")
    elif not score.resolved:
        categories.add("unresolved")
    if score.false_resolved:
        categories.add("false_resolved")
    if score.application_status in {"incident_not_created", "stimulus_failed"}:
        categories.add("platform_failure")
    if score.resolved and score.grader_status == "INCOMPLETE":
        categories.add("structured_incomplete")
    if score.resolved and score.grader_status == "FAIL":
        categories.add("structured_failure")
    if not score.safety_ok:
        categories.add("safety_failure")
    return tuple(sorted(categories))


def _record_statistical_trial(
    spec: ScenarioSpec,
    score,
    *,
    trial_index: int,
    latency_seconds: float,
) -> None:
    if not STATISTICAL_RECORDING:
        return
    pair_id = make_pair_id(
        experiment_id=EXPERIMENT_ID,
        dataset_sha256=DATASET.sha256,
        scenario=spec.name,
        scenario_version=spec.scenario_version,
        trial_index=trial_index,
        pair_seed=PAIR_SEED,
    )
    trial = build_trial_record(
        experiment_id=EXPERIMENT_ID,
        pair_id=pair_id,
        candidate_id=CANDIDATE_ID,
        config_fingerprint=CONFIG_FINGERPRINT,
        scenario=spec.name,
        scenario_version=spec.scenario_version,
        dataset_sha256=DATASET.sha256,
        risk_class=spec.risk_class,
        oracle_status=score.oracle_status,
        resolved=score.resolved,
        false_resolved=score.false_resolved,
        grader_status=score.grader_status,
        safety_ok=score.safety_ok,
        mttr_seconds=score.mttr_seconds,
        latency_seconds=latency_seconds,
        cost_usd=None,
        failure_categories=list(_failure_categories(score)),
        oracle_artifact=str(ORACLE_RESULTS_PATH),
        grader_artifact=str(GRADER_RESULTS_PATH),
    )
    append_trial(TRIAL_RESULTS_PATH, trial)


async def _run_trial(
    client: httpx.AsyncClient,
    jwt: str,
    oracle_client: PrometheusOracleClient,
    fault_adapter: Optional[MeridianAdminConfigAdapter],
    spec: ScenarioSpec,
):
    known = await _incident_ids(client, jwt)
    tracker = RecoveryOracleTracker(spec.recovery_probe, datetime.now(timezone.utc))
    await _observe_oracle(client, oracle_client, tracker, baseline=True)

    lease = None
    manual_fault_started = False
    try:
        if tracker.baseline_healthy is True and FAULT_MODE == "automatic":
            if fault_adapter is None:
                raise RuntimeError("automatic fault mode has no adapter")
            lease = await fault_adapter.inject(client, spec)
            started_at = lease.injected_at
        else:
            await _await_manual_fault(spec, tracker)
            manual_fault_started = (
                tracker.baseline_healthy is True and FAULT_MODE == "manual"
            )
            started_at = datetime.now(timezone.utc)
        tracker.begin(started_at)

        try:
            await _fire_alert(client, spec, started_at)
            await _observe_oracle(client, oracle_client, tracker)
        except Exception as exc:
            result = _oracle_result(
                tracker,
                spec,
                incident_id=None,
                application_status="stimulus_failed",
            )
            append_oracle_result(ORACLE_RESULTS_PATH, result)
            score = _score_without_output(spec, result)
            _record_grade(spec, result, "", [], score)
            return score, f"FAILED (stimulus: {exc})"

        incident = await _wait_new_incident(client, jwt, known)
        if not incident:
            result = _oracle_result(
                tracker,
                spec,
                incident_id=None,
                application_status="incident_not_created",
            )
            append_oracle_result(ORACLE_RESULTS_PATH, result)
            score = _score_without_output(spec, result)
            _record_grade(spec, result, "", [], score)
            return score, "FAILED (no incident)"

        latest_incident = await _wait_for_recovery(
            client, jwt, incident, oracle_client, tracker
        )
        transcript = await _fetch_transcript(client, jwt, incident["id"])
        summary_text = transcript.get("summary") or latest_incident.get("summary") or ""
        events = transcript.get("events", [])
        result = _oracle_result(
            tracker,
            spec,
            incident_id=incident["id"],
            application_status=str(latest_incident.get("status") or "unknown"),
        )
        append_oracle_result(ORACLE_RESULTS_PATH, result)
        score = score_run(
            spec,
            result.status,
            result.application_status,
            summary_text,
            events,
            mttr_seconds=result.mttr_seconds,
            incident_severity=latest_incident.get("severity", ""),
        )
        _record_grade(spec, result, summary_text, events, score)
        if score.resolved:
            line = (
                f"MTTR={score.mttr_seconds:.0f}s "
                f"app={score.application_status} "
                f"rc={_mark(score.root_cause_hit)} "
                f"rem={_mark(score.remediation_hit)} "
                f"sev={_mark(score.severity_hit)} "
                f"safe={_mark(score.safety_ok)}"
            )
        else:
            false_claim = " FALSE_RESOLVED" if score.false_resolved else ""
            line = (
                f"{score.oracle_status} "
                f"(app={score.application_status}){false_claim}"
            )
        return score, line
    finally:
        if lease is not None and fault_adapter is not None:
            await fault_adapter.cleanup(client, lease)
        elif manual_fault_started:
            await _await_manual_cleanup(spec)


async def run() -> None:
    print("=" * 74)
    print(
        "  SRE-Agent Benchmark  "
        "(oracle recovery · MTTR · root-cause · remediation · severity · safety)"
    )
    print(f"  {len(SCENARIOS)} scenarios × {RUNS_PER_SCENARIO} runs")
    print(
        f"  dataset: {DATASET.dataset_version}/{DATASET.split} "
        f"sha256={DATASET.sha256[:12]}…"
    )
    print(f"  fault mode: {FAULT_MODE}")
    print(f"  oracle evidence: {ORACLE_RESULTS_PATH}")
    print(f"  grader evidence: {GRADER_RESULTS_PATH}")
    if STATISTICAL_RECORDING:
        print(
            f"  experiment: {EXPERIMENT_ID}/{CANDIDATE_ID} "
            f"trials={TRIAL_RESULTS_PATH}"
        )
    print("=" * 74)

    all_scores = []
    oracle_client = PrometheusOracleClient(
        PROMETHEUS_URL, bearer_token=PROMETHEUS_TOKEN
    )
    fault_adapter = (
        MeridianAdminConfigAdapter(FAULT_SERVICE_URLS)
        if FAULT_MODE == "automatic"
        else None
    )
    async with httpx.AsyncClient(timeout=30) as client:
        jwt = await _login(client)
        print(f"  logged in as {ADMIN_EMAIL}\n")

        by_name = {spec.name: spec for spec in SCENARIOS}
        schedule = build_trial_schedule(
            list(by_name),
            runs_per_scenario=RUNS_PER_SCENARIO,
            pair_seed=PAIR_SEED or "not-recorded",
            dataset_sha256=DATASET.sha256,
            randomize=STATISTICAL_RECORDING,
        )
        for position, (scenario_name, trial_index) in enumerate(schedule, 1):
            spec = by_name[scenario_name]
            print(
                f"── {spec.name} run {trial_index}/{RUNS_PER_SCENARIO}  ",
                end="",
                flush=True,
            )
            trial_started = time.perf_counter()
            score, line = await _run_trial(
                client,
                jwt,
                oracle_client,
                fault_adapter,
                spec,
            )
            _record_statistical_trial(
                spec,
                score,
                trial_index=trial_index,
                latency_seconds=time.perf_counter() - trial_started,
            )
            all_scores.append(score)
            print(line)
            if position < len(schedule):
                await asyncio.sleep(COOLDOWN_SEC)
        print()

    _report(all_scores)


def _mark(v) -> str:
    return "—" if v is None else ("✓" if v else "✗")


def _report(scores) -> None:
    agg = aggregate(scores)
    print("=" * 74)
    print("  RESULTS")
    print("=" * 74)

    def pct(x):
        return "  n/a" if x is None else f"{x*100:5.1f}%"

    def sec(x):
        return "  n/a" if x is None else f"{x:6.0f}s"

    print(
        f"  Oracle recovery rate : {pct(agg['resolution_rate'])}   ({agg['resolved']}/{agg['runs']})"
    )
    print(f"  False-resolved claims: {agg['false_resolved']}")
    print(f"  Invalid scenarios    : {agg['invalid_scenarios']}")
    print(f"  Root-cause accuracy  : {pct(agg['root_cause_accuracy'])}")
    print(f"  Remediation accuracy : {pct(agg['remediation_accuracy'])}")
    print(f"  Severity accuracy    : {pct(agg['severity_accuracy'])}")
    print(f"  Safety rate          : {pct(agg['safety_rate'])}")
    print(
        "  Structured grades   : "
        f"{agg['structured_complete']} complete / "
        f"{agg['structured_incomplete']} incomplete / "
        f"{agg['structured_failed']} failed"
    )
    print(
        "  Oracle MTTR mean/med.: "
        f"{sec(agg['oracle_mttr_mean_s'])} / "
        f"{sec(agg['oracle_mttr_median_s'])}"
    )
    print("=" * 74)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        sys.exit(1)
