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
    BENCH_BASE_URL, BENCH_creds.admin_email, BENCH_creds.admin_password,
    BENCH_CLUSTER_ID, BENCH_CLUSTER_TOKEN, BENCH_RUNS_PER_SCENARIO,
    BENCH_PROMETHEUS_URL, BENCH_ORACLE_RESULTS_PATH
"""

import asyncio
import base64
import hashlib
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(1, str(Path(__file__).resolve().parents[1]))
from fault_adapter import MeridianAdminConfigAdapter  # noqa: E402
from fixtures import resolve_credentials  # noqa: E402
from recovery_oracle import (  # noqa: E402
    PrometheusOracleClient,
    RecoveryOracleTracker,
    append_oracle_result,
)
from scenario_dataset import load_dataset  # noqa: E402
from scoring import (  # noqa: E402
    PLATFORM_FAILURE_STATUSES,
    ScenarioSpec,
    aggregate,
    score_run,
)
from statistical_eval import (  # noqa: E402
    append_trial,
    build_trial_record,
    build_trial_schedule,
    configuration_fingerprint,
    make_pair_id,
)
from structured_grading import append_grader_record  # noqa: E402

from sre_agent.confidence_calibration import (  # noqa: E402
    append_confidence_record,
    build_confidence_record,
)

# ── Config ──────────────────────────────────────────────────────────────────
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
CONFIDENCE_RESULTS_PATH = Path(
    os.getenv(
        "BENCH_CONFIDENCE_RESULTS_PATH",
        "reports/sre-bench-confidence.jsonl",
    )
)
EXPERIMENT_ID = os.getenv("BENCH_EXPERIMENT_ID", "").strip()
CANDIDATE_ID = os.getenv("BENCH_CANDIDATE_ID", "").strip()
CONFIG_FINGERPRINT = os.getenv("BENCH_CONFIG_FINGERPRINT", "").strip()
PAIR_SEED = os.getenv("BENCH_PAIR_SEED", "").strip()

# ── The approval step the harness was missing ──────────────────────────────
#
# `awaiting_approval` is terminal in TERMINAL_APPLICATION_STATUSES and nothing
# ever clears it, so a plan the gate holds for a human ends the trial there.
# That scores a correct refusal as UNRESOLVED: on a production cluster
# `policy_gate` holds every PROD rollback and any uncalibrated mutation, which
# is the behaviour we want, and the benchmark could only ever punish it.
#
# With this on, the harness plays the human approver through the same two
# calls the dashboard makes -- GET /status for the pending approval_request_id
# and action_hash, then POST /approve. It is not a DB bypass and not a new
# code path; the graph still verifies the action_hash against the plan it is
# about to run, and the Prometheus oracle still decides recovery on its own.
#
# It is off by default, because it does change what a trial measures: with it
# the trial answers "can the agent fix this once authorized", without it
# "can the agent fix this unaided". Both are worth measuring and they are not
# the same number, so every approval granted here is counted and stamped onto
# the grade record and the trial's failure_categories. A run that needed a
# human can never be read back as one that did not.
AUTO_APPROVE = os.getenv("BENCH_AUTO_APPROVE", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
# A plan can legitimately pause more than once; a runaway loop should still
# not be able to spend the whole timeout POSTing approvals.
AUTO_APPROVE_LIMIT = int(os.getenv("BENCH_AUTO_APPROVE_LIMIT", "3"))

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
# Both record writers parse this as a lowercase SHA-256, and the confidence
# corpus is grouped by it. Without this check a typo is still fatal -- just
# fatal at the first write, which happens after an incident has been
# provisioned, investigated and paid for, and it aborts the run there. The
# shape is knowable before any of that.
if STATISTICAL_RECORDING and (
    len(CONFIG_FINGERPRINT) != 64
    or any(character not in "0123456789abcdef" for character in CONFIG_FINGERPRINT)
):
    raise RuntimeError(
        "BENCH_CONFIG_FINGERPRINT must be a lowercase 64-character SHA-256 digest; "
        f"got {len(CONFIG_FINGERPRINT)} characters. Every trial row and every "
        "confidence observation is keyed on it."
    )

# ── Confidence observations are not paired trials ──────────────────────────
#
# Both records used to hang off STATISTICAL_RECORDING, so the only runs that
# produced calibration evidence were full paired experiments -- and
# BENCH_SCENARIOS raises rather than run beside one. Every single-scenario
# trial therefore measured a real (confidence, outcome) pair and discarded it
# unwritten: five live incidents, zero samples, and a runtime that stays
# uncalibrated because the corpus it needs is never written.
#
# The two records do not need the same identity. A trial row only means
# anything against its pair in another arm, which is exactly what PAIR_SEED
# and an unfiltered split protect. A confidence observation is a single
# reliability point: the schema asks for a config fingerprint and an id, and
# nothing else -- no experiment_id, no candidate_id. A filtered smoke run
# against a real fault produces one just as honestly as a full split does.
#
# Caveat worth stating where it will be read: this corpus is grouped by task,
# not by scenario, so a run of N trials against one scenario yields N samples
# that all describe that scenario. The artifact would still clear its sample
# floors while describing far less than it appears to. Spread the corpus
# across scenarios before trusting a threshold built from it.
CONFIDENCE_RECORDING = STATISTICAL_RECORDING or os.getenv(
    "BENCH_RECORD_CONFIDENCE", "1"
).strip().lower() in {"1", "true", "yes", "on"}

# Reading a corpus rejects a duplicate (task, pair_id, config_fingerprint)
# outright rather than skipping it, so an id that repeats across runs does not
# lose one sample -- it makes the whole file unreadable. `make_pair_id` is
# deterministic in the trial index, which is 1 for every single-trial run, so
# unpaired observations mix in a per-process id to stay unique.
RUN_ID = uuid.uuid4().hex

DATASET_ROOT = Path(
    os.getenv(
        "BENCH_DATASET_ROOT",
        str(Path(__file__).resolve().parent / "datasets"),
    )
)
DATASET_VERSION = os.getenv("BENCH_DATASET_VERSION", "v2")
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
    # Not scraped by Prometheus; it is a fault target only, driving the
    # services that are scraped.
    "load-generator": os.getenv("BENCH_LOADGEN_URL", "http://localhost:8003"),
}
POLL_INTERVAL_SEC = 5
# How long one trial may wait for the oracle to see recovery. 300s was chosen
# when a run was one investigation pass; it is below the floor of what this
# system actually takes. Measured on the live cluster, a single incident spends
# 10-30 minutes in the graph: each specialist may burn its own 120s ceiling,
# and the reflector's unknowns can send the whole set round again. A ceiling
# under the loop's own runtime does not measure a slow agent, it records every
# trial as a non-recovery — so the ceiling is operator-set, and the default
# stays 300 only so existing invocations keep their meaning.
TIMEOUT_SEC = int(os.getenv("BENCH_INCIDENT_TIMEOUT_SEC", "300"))
# The fastest complete investigation ever measured on the live cluster: Phase 0
# on 2026-09-19, 110 LLM calls in 21.1 minutes. A ceiling below even the
# quickest observed run cannot be waiting for a real outcome, so the header
# says so out loud. Deliberately the measured *floor* and not the 49-minute
# ceiling — this flags invocations that cannot work, not ones that might be
# tight. Three pilots on 2026-09-19 were silently voided by this before anyone
# noticed the default was still 300.
MEASURED_INCIDENT_FLOOR_SEC = 1260


def timeout_warning(fault_mode: str, timeout_sec: int) -> str | None:
    """The header's warning when the ceiling makes recovery unmeasurable.

    Not an error, and deliberately not fatal: the ceiling is operator-set on
    purpose, and a short one is legitimate when smoke-testing the harness
    itself or running with no fault at all. It is fatal to a *measurement*,
    though, and silently so — the trial records a non-recovery whatever the
    agent did, and cleanup pulls the fault while the agent is still looking
    at it, so the tail of the investigation is spent on a fault that is no
    longer there and any plan produced after that point is evidence about
    nothing.
    """
    if fault_mode == "none" or timeout_sec >= MEASURED_INCIDENT_FLOOR_SEC:
        return None
    return (
        f"  ⚠ WARNING: {timeout_sec}s is below the {MEASURED_INCIDENT_FLOOR_SEC}s "
        f"({MEASURED_INCIDENT_FLOOR_SEC // 60} min) floor measured on this cluster.\n"
        f"    Every trial will record a non-recovery regardless of agent behaviour,\n"
        f"    and fault cleanup will fire mid-investigation. This run cannot\n"
        f"    measure recovery. Set BENCH_INCIDENT_TIMEOUT_SEC=2700 for a real one."
    )


COOLDOWN_SEC = 30
ORACLE_COMPLETION_GRACE_SEC = int(os.getenv("BENCH_ORACLE_COMPLETION_GRACE_SEC", "30"))
ACCOUNTING_WAIT_SEC = int(os.getenv("BENCH_ACCOUNTING_WAIT_SECONDS", "30"))

# Statuses the graph stops at, used only to stop polling early — recovery
# itself is decided by the Prometheus probe, never by these.
#
# `pending_acknowledgment` belongs here and was missing: it is where a *verified
# autonomous fix* lands, and only a human running the Slack "acknowledge"
# command moves it to `resolved`, which no benchmark run does. Every terminal
# failure status was listed and the one success status was not, so a trial in
# which the agent actually fixed something polled for the full incident timeout
# whenever the probe had not yet cleared. `remediation_in_progress` stays out:
# verification has not run yet, so it is genuinely transient.
TERMINAL_APPLICATION_STATUSES = {
    "investigated",
    "awaiting_approval",
    "pending_acknowledgment",
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

# A smoke knob, not a measurement one. One incident on this cluster costs real
# money and 10-30 minutes, so "does the ACT path work at all" should not have
# to buy a whole split to find out.
#
# It is refused during a recorded experiment on purpose. `build_trial_schedule`
# derives run order from the *list of names it is handed*, so a filtered arm
# and an unfiltered one would be ordered differently under the same
# `BENCH_PAIR_SEED` — the seed exists to hold run order constant across arms,
# and a subset silently removes that guarantee while still stamping every trial
# with the full split's `dataset_sha256`. The resulting artifact would claim a
# comparability it does not have.
SCENARIO_FILTER = tuple(
    name.strip() for name in os.getenv("BENCH_SCENARIOS", "").split(",") if name.strip()
)
if SCENARIO_FILTER:
    if STATISTICAL_RECORDING:
        raise RuntimeError(
            "BENCH_SCENARIOS is a smoke-run filter and cannot be combined with "
            "statistical recording: a subset breaks the run-order guarantee "
            "BENCH_PAIR_SEED provides across arms, while the trials would still "
            "carry the full split's dataset_sha256. Run the whole split, or drop "
            "the statistical env (BENCH_EXPERIMENT_ID/BENCH_CANDIDATE_ID/…)."
        )
    _known = {spec.name for spec in SCENARIOS}
    _unknown = sorted(set(SCENARIO_FILTER) - _known)
    if _unknown:
        # Degrading to the full split would turn a typo into a bill.
        raise RuntimeError(
            f"BENCH_SCENARIOS names {_unknown} which are not in "
            f"{DATASET.dataset_version}/{DATASET.split}. Available: "
            f"{sorted(_known)}"
        )
    SCENARIOS = [spec for spec in SCENARIOS if spec.name in SCENARIO_FILTER]


async def _login(client: httpx.AsyncClient, creds) -> str:
    r = await client.post(
        f"{creds.base_url}/auth/token",
        data={"username": creds.admin_email, "password": creds.admin_password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    r.raise_for_status()
    return r.json()["access_token"]


# Renew once this much of the token's life is gone. Well clear of the expiry
# even if a poll is slow, without re-logging in on every request.
_TOKEN_RENEW_FRACTION = 0.6
# Used only when the token carries no readable `exp`. Deliberately short: the
# cost of renewing too often is one cheap request, the cost of renewing too
# late is a dead campaign.
_TOKEN_FALLBACK_LIFETIME = timedelta(minutes=5)


def _token_lifetime(token: str, issued_at: datetime) -> Optional[timedelta]:
    """How long this JWT has left, read from its own unverified `exp` claim.

    Reading a token without verifying it is normally a mistake. Here it is
    scheduling, not authentication: the server remains the only thing that
    decides whether a token is accepted, and the worst a forged `exp` could do
    is make this process renew at the wrong moment. Scheduling from the claim
    rather than a constant means a deployment that changes
    `ACCESS_TOKEN_EXPIRE_MINUTES` does not silently reintroduce the bug below.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload))["exp"]
        return datetime.fromtimestamp(exp, tz=timezone.utc) - issued_at
    except Exception:
        return None


class _Token:
    """An access token that renews itself before the server stops taking it.

    The runner used to log in once and reuse that string for the whole
    campaign. Access tokens live 15 minutes (`ACCESS_TOKEN_EXPIRE_MINUTES`)
    while a single incident is allowed 45 (`BENCH_INCIDENT_TIMEOUT_SEC`), so
    any incident that ran longer than its token died mid-poll on a 401 —
    after the fault was injected and the agent had already spent the money.
    Nothing was scored and the fault was left in the cluster. Long runs are
    the only runs this benchmark exists to do, so the single login was a bug
    that grew with the value of the run.
    """

    def __init__(self, client: httpx.AsyncClient, creds) -> None:
        self._client = client
        self._creds = creds
        self._value: Optional[str] = None
        self._renew_at = datetime.min.replace(tzinfo=timezone.utc)

    async def value(self) -> str:
        if self._value is None or datetime.now(timezone.utc) >= self._renew_at:
            await self._renew()
        assert self._value is not None
        return self._value

    async def headers(self) -> dict:
        return {"Authorization": f"Bearer {await self.value()}"}

    async def _renew(self) -> None:
        issued_at = datetime.now(timezone.utc)
        value = await _login(self._client, self._creds)
        lifetime = _token_lifetime(value, issued_at) or _TOKEN_FALLBACK_LIFETIME
        if lifetime <= timedelta(0):
            # Renewing on a schedule already in the past would turn every
            # request into a login. Stop instead of hammering /auth/token.
            raise RuntimeError(
                "the platform issued an already-expired access token "
                f"(exp is {-lifetime} in the past); check clock skew between "
                "this host and the API before running a campaign"
            )
        self._value = value
        self._renew_at = issued_at + lifetime * _TOKEN_RENEW_FRACTION


async def _incident_ids(client: httpx.AsyncClient, jwt: _Token, creds) -> set[str]:
    r = await client.get(
        f"{creds.base_url}/api/v1/clusters/{creds.cluster_id}/incidents",
        headers=await jwt.headers(),
    )
    r.raise_for_status()
    return {inc["id"] for inc in r.json()}


@dataclass(frozen=True)
class AlertReceipt:
    """What the platform did with the alert this trial fired.

    The webhook reports in its own response body how many incidents it created,
    how many it folded into an already-open incident, and how many resolved
    alerts it reconciled. The harness used to discard that body, which left it
    able to observe only "no new incident appeared" -- so when it had to say why,
    it stated a fixed guess instead. See `_diagnose_missing_incident`.
    """

    created: int
    folded: int
    reconciled: int
    raw: dict

    @property
    def absorbed(self) -> bool:
        """Delivered and accepted, but it opened no incident of its own."""
        return self.created == 0


async def _fire_alert(
    client: httpx.AsyncClient, spec: ScenarioSpec, started_at: datetime, creds
) -> AlertReceipt:
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
        f"{creds.base_url}/api/v1/alerts/webhook",
        json=payload,
        headers={"Authorization": f"Bearer {creds.cluster_token}"},
    )
    r.raise_for_status()
    # A malformed or absent body must not fail a trial that the platform
    # accepted: the receipt is diagnostic, not part of the stimulus contract.
    try:
        body = r.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    return AlertReceipt(
        created=int(body.get("incidents_created") or 0),
        folded=int(body.get("incidents_folded") or 0),
        reconciled=int(body.get("resolved_reconciled") or 0),
        raw=body,
    )


# meridian's own api-gateway OOMKills without anyone asking it to, so "an
# incident that was not here before" is not the same claim as "the incident
# this scenario caused". Binding the wrong one grades a trial against a
# scenario it never ran.
#
# One global number, deliberately, rather than something derived per scenario
# from its alert's `for:` duration. The harness posts the alert itself
# (`_fire_alert`) and the platform opens the incident synchronously inside that
# request, so Prometheus detection latency never enters this wait: across the
# 2026-09-24 campaign every incident's `created_at` equals its trial's oracle
# `started_at` to the second. A missing incident is therefore never a slow one,
# and a longer window cannot produce one -- what it actually means is worked out
# by `_diagnose_missing_incident`.
INCIDENT_WAIT_SEC = int(os.getenv("BENCH_INCIDENT_WAIT_SECONDS", "60"))

# Mirrors `_FOLD_WINDOW_MINUTES` in `sre_agent/api/v1/alerts.py`. Used only to
# name a likely absorber in a diagnostic message, never to decide a verdict.
FOLD_WINDOW_MINUTES = 120


def _matches_scenario(incident: dict, spec: Optional[ScenarioSpec]) -> bool:
    """Whether this incident is the one `spec`'s alert opened.

    Incident titles are written as ``[service] AlertName``, and the alertname
    is what the webhook dedups on, so it is the field that actually
    identifies the stimulus. With no spec to check against, every new
    incident qualifies -- the old behaviour, kept for callers that have no
    scenario in hand.
    """
    if spec is None:
        return True
    alertname = str(spec.alert.get("alertname", "")).strip()
    if not alertname:
        return True
    return alertname.lower() in str(incident.get("title", "")).lower()


async def _wait_new_incident(
    client, jwt, known, creds, spec: Optional[ScenarioSpec] = None
) -> Optional[dict]:
    """The incident this scenario's alert opened, or None if it opened none.

    None is a real answer, not only a slow one: the platform dedups a firing
    alert into an already-open incident with the same identity and discards
    it (`sre_agent/api/v1/alerts.py`). A stale incident left open by an
    earlier campaign therefore makes its scenario unrunnable until that
    incident is closed, and no wait however long will produce a new one.
    """
    deadline = asyncio.get_running_loop().time() + INCIDENT_WAIT_SEC
    unrelated: set[str] = set()
    while True:
        await asyncio.sleep(2)
        r = await client.get(
            f"{creds.base_url}/api/v1/clusters/{creds.cluster_id}/incidents",
            headers=await jwt.headers(),
        )
        r.raise_for_status()
        for inc in r.json():
            if inc["id"] in known:
                continue
            if _matches_scenario(inc, spec):
                return inc
            if inc["id"] not in unrelated:
                unrelated.add(inc["id"])
                print(
                    f"     ignoring unrelated incident {str(inc.get('title'))[:48]!r}"
                )
        if asyncio.get_running_loop().time() >= deadline:
            return None


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _fold_candidate(open_incidents: list, service: str) -> Optional[dict]:
    """The open same-service incident this alert could have folded into.

    Follows `_find_fold_target` in `sre_agent/api/v1/alerts.py` only as far as
    naming a suspect -- same service, opened inside the fold window, most recent
    first. It deliberately does not re-implement that function's parked-status
    conditions: duplicating a safety rule in the harness invites the two copies
    to drift, and a named candidate the harness cannot fully confirm is already
    far better evidence than the fixed guess it replaces.
    """
    if not service:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=FOLD_WINDOW_MINUTES)
    best: Optional[dict] = None
    best_at: Optional[datetime] = None
    prefix = f"[{service.lower()}]"
    for inc in open_incidents:
        if not str(inc.get("title", "")).lower().startswith(prefix):
            continue
        created = _parse_ts(inc.get("created_at"))
        if created is None or created < cutoff:
            continue
        if best_at is None or created > best_at:
            best, best_at = inc, created
    return best


async def _diagnose_missing_incident(
    client, jwt, creds, spec: Optional[ScenarioSpec],
    receipt: Optional[AlertReceipt],
) -> tuple[str, str]:
    """Why this scenario's alert opened no incident. Observed, not assumed.

    Returns `(application_status, reason)`.

    This replaces a fixed string that asserted one cause -- "an already-open
    incident with this alertname would have deduped it away" -- without ever
    checking. The cost of that guess was not hypothetical: when
    `checkout_memory_leak_oom` produced no incident in both arms of the
    2026-09-25 campaign, its attestation recorded a *different* unverified cause
    (a memory threshold the heap could not reach in the window) as "root cause
    proven, not inferred". Both explanations were reached without evidence, and
    the alert's real fate was recorded nowhere, because the one authoritative
    answer -- the webhook's own response body -- was being thrown away.

    The heap explanation could not have been right: the harness fires the alert
    itself, so the Prometheus rule's threshold and `for:` duration have no say in
    whether an incident opens.
    """
    counts = ""
    if receipt is not None:
        counts = (
            f" [webhook receipt: created={receipt.created}"
            f" folded={receipt.folded} reconciled={receipt.reconciled}]"
        )

    alert = (spec.alert if spec else None) or {}
    alertname = str(alert.get("alertname", "")).strip()
    service = str(alert.get("service", "")).strip()
    title = f"[{service}] {alertname}" if service and alertname else ""

    try:
        r = await client.get(
            f"{creds.base_url}/api/v1/clusters/{creds.cluster_id}/incidents",
            headers=await jwt.headers(),
        )
        r.raise_for_status()
        open_incidents = [
            inc
            for inc in r.json()
            if str(inc.get("status", "")).lower() != "resolved"
        ]
    except Exception as exc:
        return (
            "incident_not_created",
            "no incident appeared, and the incident list could not be read to "
            f"say why ({type(exc).__name__}: {exc}).{counts}",
        )

    exact = next(
        (
            inc
            for inc in open_incidents
            if title and str(inc.get("title", "")).strip().lower() == title.lower()
        ),
        None,
    )
    if exact is not None:
        return (
            "incident_absorbed",
            f"deduped into already-open incident {str(exact.get('id'))[:8]} "
            f"({exact.get('status')}, opened {str(exact.get('created_at'))[:19]}) "
            f"carrying the same title {title!r}.{counts}",
        )

    fold = _fold_candidate(open_incidents, service)
    if fold is not None:
        return (
            "incident_absorbed",
            f"folded into open same-service incident {str(fold.get('id'))[:8]} "
            f"({fold.get('status')}, opened {str(fold.get('created_at'))[:19]}, "
            f"title {str(fold.get('title'))!r}), inside the "
            f"{FOLD_WINDOW_MINUTES}-minute same-service fold window.{counts}",
        )

    if receipt is not None and receipt.folded:
        return (
            "incident_absorbed",
            "the platform folded this alert into an existing incident that is "
            f"no longer open, so it cannot be named here.{counts}",
        )

    return (
        "incident_not_created",
        "the alert was delivered but the platform created, folded and "
        "reconciled nothing, and no open incident can account for it. The "
        "stimulus reached the platform and vanished inside it." + counts,
    )


async def _fetch_incident(client, jwt, incident_id, creds) -> Optional[dict]:
    response = await client.get(
        f"{creds.base_url}/api/v1/clusters/{creds.cluster_id}/incidents",
        headers=await jwt.headers(),
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
    steps = "; ".join(
        f"apply {contract['inject']} to {contract['target']}"
        for contract in spec.fault["contracts"]
    )
    restore = _manual_cleanup_steps(spec)
    prompt = (
        f"\nBaseline is healthy for {spec.name}. In order: {steps}. "
        f"Then press Enter. Required cleanup: {restore}. "
    )
    await asyncio.to_thread(input, prompt)


def _manual_cleanup_steps(spec: ScenarioSpec) -> str:
    """Restoration steps in reverse application order, as the adapter unwinds."""
    return "; ".join(
        f"restore {contract['target']} to {contract['cleanup']}"
        for contract in reversed(spec.fault["contracts"])
    )


async def _await_manual_cleanup(spec: ScenarioSpec) -> None:
    if FAULT_MODE != "manual":
        return
    await asyncio.to_thread(
        input,
        f"\n{_manual_cleanup_steps(spec)}, verify each, then press Enter. ",
    )


async def _grant_pending_approval(
    client: httpx.AsyncClient,
    jwt: _Token,
    incident_id: str,
    creds,
    already_granted: set[str],
) -> bool:
    """Approve one paused action the way the dashboard does, or return False.

    GET /status carries the approval_request_id and action_hash of the pending
    interrupt; POST /approve resumes that exact action. The hash is the graph's
    guarantee that what a human authorized is what runs, so it is read from the
    live interrupt and echoed back rather than reconstructed here.
    """
    try:
        status = await client.get(
            f"{creds.base_url}/api/v1/incidents/{incident_id}/status",
            headers=await jwt.headers(),
        )
        status.raise_for_status()
        approval = (status.json() or {}).get("approval")
    except Exception as exc:
        print(f"[auto-approve] status read failed: {exc}  ", end="", flush=True)
        return False

    if not isinstance(approval, dict):
        return False
    request_id = str(approval.get("approval_request_id") or "")
    action_hash = str(approval.get("action_hash") or "")
    if not request_id or not action_hash:
        return False
    # The graph republishes the same interrupt until it is decided; approving
    # it twice would 409 and, worse, would double-count in the grade record.
    if action_hash in already_granted:
        return False

    try:
        response = await client.post(
            f"{creds.base_url}/api/v1/incidents/{incident_id}/approve",
            headers=await jwt.headers(),
            json={"approval_request_id": request_id, "action_hash": action_hash},
            # The endpoint resumes the graph synchronously, so this call is as
            # long as the remediation it authorizes.
            timeout=httpx.Timeout(TIMEOUT_SEC),
        )
    except Exception as exc:
        print(f"[auto-approve] POST failed: {exc}  ", end="", flush=True)
        return False

    if response.status_code >= 400:
        detail = response.text[:160]
        print(
            f"[auto-approve] refused {response.status_code}: {detail}  ",
            end="",
            flush=True,
        )
        return False

    already_granted.add(action_hash)
    print(f"[auto-approve] granted {action_hash[:12]}…  ", end="", flush=True)
    return True


async def _wait_for_recovery(
    client: httpx.AsyncClient,
    jwt: _Token,
    incident: dict,
    oracle_client: PrometheusOracleClient,
    tracker: RecoveryOracleTracker,
    creds,
) -> tuple[dict, int]:
    """Poll independent evidence; application status is context, never the oracle.

    Returns the latest incident and the number of approvals the harness itself
    granted, which the caller records so an authorized run stays distinguishable
    from an autonomous one.
    """
    elapsed = 0
    terminal_seen_at: Optional[int] = None
    latest = incident
    granted_hashes: set[str] = set()

    while elapsed < TIMEOUT_SEC:
        await asyncio.sleep(POLL_INTERVAL_SEC)
        elapsed += POLL_INTERVAL_SEC
        await _observe_oracle(client, oracle_client, tracker)

        current = await _fetch_incident(client, jwt, incident["id"], creds)
        if current is not None:
            latest = current
        application_status = str(latest.get("status") or "").lower()

        # An unarmed probe (`require_failure_observation: false`) can report
        # recovery on its very first observation, because for a negative
        # control the healthy band is where the metric already sits. Breaking
        # on that tore the trial down seconds after the alert fired, before the
        # agent had emitted a single span, and the run was then graded
        # INSUFFICIENT_EVIDENCE for a question it was never given time to
        # answer. Wait for the investigation to finish instead. Armed probes
        # are unaffected: they cannot set `recovered_at` at all without having
        # observed the failure first.
        if tracker.recovered_at is not None and tracker.failure_observed:
            break
        if (
            AUTO_APPROVE
            and application_status == "awaiting_approval"
            and len(granted_hashes) < AUTO_APPROVE_LIMIT
        ):
            if await _grant_pending_approval(
                client, jwt, incident["id"], creds, granted_hashes
            ):
                # The run is moving again: it is no longer sitting on a
                # terminal status, so the grace countdown has to start over.
                terminal_seen_at = None
                continue
        if application_status in TERMINAL_APPLICATION_STATUSES:
            if terminal_seen_at is None:
                terminal_seen_at = elapsed
            elif elapsed - terminal_seen_at >= ORACLE_COMPLETION_GRACE_SEC:
                break
        else:
            terminal_seen_at = None

    return latest, len(granted_hashes)


async def _fetch_transcript(client, jwt, incident_id, creds) -> dict:
    r = await client.get(
        f"{creds.base_url}/api/v1/incidents/{incident_id}/transcript",
        headers=await jwt.headers(),
    )
    r.raise_for_status()
    return r.json()


async def _fetch_trace_completeness(client, jwt, incident_id, creds) -> dict:
    deadline = time.monotonic() + ACCOUNTING_WAIT_SEC
    while True:
        response = await client.get(
            f"{creds.base_url}/api/v1/incidents/{incident_id}/agent-metrics",
            headers=await jwt.headers(),
        )
        response.raise_for_status()
        payload = response.json().get("trace_completeness")
        trace = payload if isinstance(payload, dict) else {}
        reasons = trace.get("completeness_reasons") or []
        still_running = trace.get("root_trace_id") is None or any(
            reason == "root_span_not_finalized" for reason in reasons
        )
        if not still_running or time.monotonic() >= deadline:
            return trace
        await asyncio.sleep(min(POLL_INTERVAL_SEC, 1))


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
    harness_approvals: int = 0,
) -> None:
    append_grader_record(
        GRADER_RESULTS_PATH,
        spec=spec,
        oracle_status=result.status,
        application_status=result.application_status,
        summary_text=summary_text,
        events=events,
        score=score,
        harness_approvals=harness_approvals,
    )


def _failure_categories(score) -> tuple[str, ...]:
    categories: set[str] = set()
    if score.oracle_status == "INVALID_SCENARIO":
        categories.add("invalid_scenario")
    elif not score.resolved:
        categories.add("unresolved")
    if score.false_resolved:
        categories.add("false_resolved")
    if score.application_status in PLATFORM_FAILURE_STATUSES:
        categories.add("platform_failure")
    if score.resolved and score.grader_status == "INCOMPLETE":
        categories.add("structured_incomplete")
    if score.resolved and score.grader_status == "FAIL":
        categories.add("structured_failure")
    if not score.safety_ok:
        categories.add("safety_failure")
    return tuple(sorted(categories))


def _diagnosis_status(score) -> str:
    """Read the diagnosis criterion and fail closed on absent/malformed grades."""
    grade = getattr(score, "structured_grade", None)
    criteria = grade.get("criteria") if isinstance(grade, dict) else None
    diagnosis = criteria.get("diagnosis") if isinstance(criteria, dict) else None
    state = diagnosis.get("state") if isinstance(diagnosis, dict) else None
    if state in {
        "PASS",
        "FAIL",
        "INSUFFICIENT_EVIDENCE",
        "REQUIRES_CALIBRATION",
        "NOT_APPLICABLE",
    }:
        return state
    return "INSUFFICIENT_EVIDENCE"


def _record_statistical_trial(
    spec: ScenarioSpec,
    score,
    *,
    trial_index: int,
    latency_seconds: float,
    trace_completeness: Optional[dict],
    harness_approvals: int = 0,
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
    trace_complete = bool(
        isinstance(trace_completeness, dict)
        and trace_completeness.get("complete") is True
    )
    cost_usd = trace_completeness.get("cost_usd") if trace_complete else None
    failure_categories = set(_failure_categories(score))
    if not trace_complete:
        failure_categories.add("trace_incomplete")
    if harness_approvals:
        # Not a failure, but the trial schema is strict about its keys and this
        # is the one free-form field in it. An arm that was authorized by the
        # harness must never be compared against one that ran unaided without
        # that being visible in the artifact people actually diff.
        failure_categories.add("harness_approved")
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
        diagnosis_status=_diagnosis_status(score),
        safety_ok=score.safety_ok,
        mttr_seconds=score.mttr_seconds,
        latency_seconds=latency_seconds,
        cost_usd=cost_usd,
        trace_complete=trace_complete,
        trace_span_count=(
            int(trace_completeness.get("spans", 0))
            if isinstance(trace_completeness, dict)
            else 0
        ),
        trace_evidence_sha256=(
            trace_completeness.get("records_sha256")
            if isinstance(trace_completeness, dict)
            else None
        ),
        trace_evidence_artifact=(
            trace_completeness.get("artifact_path")
            if isinstance(trace_completeness, dict)
            else None
        ),
        failure_categories=sorted(failure_categories),
        oracle_artifact=str(ORACLE_RESULTS_PATH),
        grader_artifact=str(GRADER_RESULTS_PATH),
    )
    append_trial(TRIAL_RESULTS_PATH, trial)


def _confidence_fingerprint() -> str:
    """The paired experiment's fingerprint when there is one, else a digest of
    the config that actually shapes a run.

    Derived rather than typed. The corpus is grouped by this value, so an
    operator who guesses one silently pools observations from configurations
    that are not comparable; one who forgets it records nothing at all. Neither
    is possible if the run computes it from itself.
    """
    if CONFIG_FINGERPRINT:
        return CONFIG_FINGERPRINT
    material = json.dumps(
        {
            "dataset_version": DATASET.dataset_version,
            "dataset_split": DATASET.split,
            "dataset_sha256": DATASET.sha256,
            "fault_mode": FAULT_MODE,
            "incident_timeout_sec": TIMEOUT_SEC,
            "auto_approve": AUTO_APPROVE,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _record_confidence_observations(
    spec: ScenarioSpec,
    score,
    *,
    trial_index: int,
) -> None:
    """Pair task-specific self-confidence with exact structured outcomes."""
    if not CONFIDENCE_RECORDING:
        return
    pair_id = make_pair_id(
        experiment_id=EXPERIMENT_ID or f"unpaired-{RUN_ID}",
        dataset_sha256=DATASET.sha256,
        scenario=spec.name,
        scenario_version=spec.scenario_version,
        trial_index=trial_index,
        pair_seed=PAIR_SEED or RUN_ID,
    )
    for task, confidence, outcome in (
        (
            "diagnosis",
            score.diagnosis_confidence,
            score.diagnosis_confidence_outcome,
        ),
        (
            "remediation",
            score.remediation_confidence,
            score.remediation_confidence_outcome,
        ),
    ):
        if confidence is None or outcome is None:
            continue
        append_confidence_record(
            CONFIDENCE_RESULTS_PATH,
            build_confidence_record(
                task=task,
                rubric_version=score.rubric_version,
                raw_confidence=confidence,
                outcome=outcome,
                scenario=spec.name,
                scenario_version=spec.scenario_version,
                dataset_sha256=DATASET.sha256,
                config_fingerprint=_confidence_fingerprint(),
                pair_id=pair_id,
                observed_at=datetime.now(timezone.utc),
                # This runner is the only sanctioned producer of the evidence
                # that may unlock autonomy: a real agent against a real fault.
                evidence_source="live_benchmark",
            ),
        )


# `TERMINAL_APPLICATION_STATUSES` means "this investigation stopped", which
# is not the same as "this incident is closed". A parked incident still dedups
# the next identical alert, and one whose investigation is still live still
# folds in the next alert for the same service.
_CLOSED_APPLICATION_STATUSES = {"resolved", "closed"}


async def _release_incident(
    client: httpx.AsyncClient,
    jwt: _Token,
    incident_id: Optional[str],
    creds,
    notes: list[str],
) -> None:
    """Close the incident this trial opened, before the next scenario fires.

    A trial that ends with its incident still open leaves that incident in the
    next scenario's way. If its investigation is still running, the next alert
    for the same service folds into it and that scenario never gets an incident
    at all; if the next trial is a second run of the same scenario, the
    identical alert title is deduped away instead. Either way a trial is lost,
    and which trials are lost depends on timing rather than on the arm, so the
    damage is not even symmetric between the arms being compared.

    This is the same class of operator action the harness already performs for
    approvals: a sanctioned API call, never a database write. `mark-resolved`
    also cancels any in-flight investigation, so the agent is not left spending
    tokens on a scenario nobody is measuring any more.
    """
    if not incident_id:
        return
    try:
        current = await _fetch_incident(client, jwt, incident_id, creds)
    except Exception as exc:
        notes.append(f"teardown: could not read incident {incident_id[:8]}: {exc}")
        return
    if current is None:
        return
    status = str(current.get("status") or "").lower()
    if status in _CLOSED_APPLICATION_STATUSES:
        return
    try:
        response = await client.post(
            f"{creds.base_url}/api/v1/incidents/{incident_id}/mark-resolved",
            headers=await jwt.headers(),
        )
        response.raise_for_status()
    except Exception as exc:
        notes.append(f"teardown: could not close incident {incident_id[:8]}: {exc}")
        return
    notes.append(
        f"teardown: closed incident {incident_id[:8]} (was "
        f"{status or 'unknown'}) so it cannot absorb the next scenario's alert"
    )


async def _fetch_run_manifest(
    client: httpx.AsyncClient,
    jwt: _Token,
    incident_id: str,
    creds,
) -> Optional[dict]:
    """Return the run manifest recorded for this incident's job, if any."""
    response = await client.get(
        f"{creds.base_url}/api/v1/clusters/{creds.cluster_id}/jobs",
        headers=await jwt.headers(),
    )
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list):
        return None
    job = next(
        (
            row
            for row in rows
            if isinstance(row, dict)
            and str(row.get("incident_id") or "") == str(incident_id)
        ),
        None,
    )
    if job is None:
        return None
    embedded = job.get("run_manifest")
    if isinstance(embedded, dict) and isinstance(embedded.get("manifest"), dict):
        return embedded["manifest"]
    job_id = job.get("id")
    if not job_id:
        return None
    detail = await client.get(
        f"{creds.base_url}/api/v1/clusters/{creds.cluster_id}"
        f"/jobs/{job_id}/manifest",
        headers=await jwt.headers(),
    )
    if detail.status_code == 404:
        return None
    detail.raise_for_status()
    payload = detail.json()
    if not isinstance(payload, dict):
        return None
    inner = payload.get("manifest")
    return inner if isinstance(inner, dict) else payload


async def _verify_declared_fingerprint(
    client: httpx.AsyncClient,
    jwt: _Token,
    incident_id: Optional[str],
    creds,
) -> None:
    """Stop the campaign if the declared fingerprint is not what actually ran.

    `BENCH_CONFIG_FINGERPRINT` is operator-declared, and import-time validation
    only checks that it is 64 hex characters. The first thing that compares it
    against a real run manifest is `benchmarks/ablation_eval.py`, which runs
    after every trial has already been paid for -- so a value that is merely
    well-formed invalidates a whole campaign retroactively. Checking it against
    the first trial's manifest makes that mistake cost one trial instead.

    An unavailable manifest is not evidence of a mismatch (the job may still be
    running), so that case warns and continues. Only a manifest that actually
    disagrees stops the campaign.
    """
    if not CONFIG_FINGERPRINT or not incident_id:
        return
    try:
        manifest = await _fetch_run_manifest(client, jwt, incident_id, creds)
    except Exception as exc:
        print(f"[fingerprint] could not verify the declared value: {exc}")
        return
    if manifest is None:
        print(
            "[fingerprint] no run manifest for the first trial yet; the "
            "declared value stays unverified until attestation"
        )
        return
    try:
        actual = configuration_fingerprint(manifest)
    except Exception as exc:
        print(f"[fingerprint] the first trial's manifest cannot be hashed: {exc}")
        return
    if actual == CONFIG_FINGERPRINT:
        print(
            "[fingerprint] declared value verified against the first trial's "
            f"manifest ({actual[:12]})"
        )
        return
    raise SystemExit(
        "BENCH_CONFIG_FINGERPRINT does not describe the configuration that "
        f"ran: declared {CONFIG_FINGERPRINT}, the first trial's manifest "
        f"hashes to {actual}. Every trial recorded under the declared value "
        "would fail arm attestation, so this campaign is stopping after one "
        "trial rather than after all of them. Re-declare the fingerprint from "
        "a real manifest (benchmarks/ablation/README.md) and start again."
    )


async def _run_trial(
    client: httpx.AsyncClient,
    jwt: _Token,
    oracle_client: PrometheusOracleClient,
    fault_adapter: Optional[MeridianAdminConfigAdapter],
    spec: ScenarioSpec,
    creds,
    trial_meta: Optional[dict[str, Any]] = None,
):
    # Facts the caller needs even when the trial raises: which incident this
    # trial opened, and what teardown did about it. The return value cannot
    # carry them, because `finally` runs after the return value is already
    # fixed.
    meta = trial_meta if trial_meta is not None else {}
    meta["notes"] = []
    known = await _incident_ids(client, jwt, creds)
    tracker = RecoveryOracleTracker(spec.recovery_probe, datetime.now(timezone.utc))
    await _observe_oracle(client, oracle_client, tracker, baseline=True)

    leases: tuple[Any, ...] = ()
    manual_fault_started = False
    try:
        if tracker.baseline_healthy is True and FAULT_MODE == "automatic":
            if fault_adapter is None:
                raise RuntimeError("automatic fault mode has no adapter")
            leases = await fault_adapter.inject(client, spec)
            # A multi-contract scenario is only fully degraded once its last
            # contract lands, so the run starts from the newest lease.
            started_at = max(lease.injected_at for lease in leases)
        else:
            await _await_manual_fault(spec, tracker)
            manual_fault_started = (
                tracker.baseline_healthy is True and FAULT_MODE == "manual"
            )
            started_at = datetime.now(timezone.utc)
        tracker.begin(started_at)

        receipt: Optional[AlertReceipt] = None
        try:
            receipt = await _fire_alert(client, spec, started_at, creds)
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
            return score, f"FAILED (stimulus: {exc})", None, 0

        incident = await _wait_new_incident(client, jwt, known, creds, spec)
        meta["incident_id"] = (incident or {}).get("id")
        if not incident:
            application_status, reason = await _diagnose_missing_incident(
                client, jwt, creds, spec, receipt
            )
            result = _oracle_result(
                tracker,
                spec,
                incident_id=None,
                application_status=application_status,
            )
            append_oracle_result(ORACLE_RESULTS_PATH, result)
            score = _score_without_output(spec, result)
            _record_grade(spec, result, "", [], score)
            return score, (
                f"FAILED (no incident in {INCIDENT_WAIT_SEC}s: {reason})"
            ), None, 0

        latest_incident, harness_approvals = await _wait_for_recovery(
            client, jwt, incident, oracle_client, tracker, creds
        )
        transcript = await _fetch_transcript(client, jwt, incident["id"], creds)
        trace_completeness = await _fetch_trace_completeness(
            client, jwt, incident["id"], creds
        )
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
        _record_grade(spec, result, summary_text, events, score, harness_approvals)
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
        if harness_approvals:
            line += f" approved-by-harness×{harness_approvals}"
        return score, line, trace_completeness, harness_approvals
    finally:
        if leases and fault_adapter is not None:
            await fault_adapter.cleanup(client, leases)
        elif manual_fault_started:
            await _await_manual_cleanup(spec)
        await _release_incident(
            client, jwt, meta.get("incident_id"), creds, meta["notes"]
        )


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
    if SCENARIO_FILTER:
        print(
            f"  SMOKE RUN — {len(SCENARIOS)} of {len(DATASET.scenarios)} scenarios "
            f"(BENCH_SCENARIOS); not a measurement of this split"
        )
    print(f"  fault mode: {FAULT_MODE}")
    print(f"  incident timeout: {TIMEOUT_SEC}s (BENCH_INCIDENT_TIMEOUT_SEC)")
    unmeasurable = timeout_warning(FAULT_MODE, TIMEOUT_SEC)
    if unmeasurable:
        print(unmeasurable)
    print(f"  oracle evidence: {ORACLE_RESULTS_PATH}")
    print(f"  grader evidence: {GRADER_RESULTS_PATH}")
    if STATISTICAL_RECORDING:
        print(
            f"  experiment: {EXPERIMENT_ID}/{CANDIDATE_ID} "
            f"trials={TRIAL_RESULTS_PATH}"
        )
        print(f"  confidence evidence: {CONFIDENCE_RESULTS_PATH}")
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
        creds = await resolve_credentials(client)
        jwt = _Token(client, creds)
        print(f"  logged in as {creds.admin_email}\n")

        by_name = {spec.name: spec for spec in SCENARIOS}
        schedule = build_trial_schedule(
            list(by_name),
            runs_per_scenario=RUNS_PER_SCENARIO,
            pair_seed=PAIR_SEED or "not-recorded",
            dataset_sha256=DATASET.sha256,
            randomize=STATISTICAL_RECORDING,
        )
        # `BENCH_CONFIG_FINGERPRINT` is a declaration until something checks
        # it against a manifest. Do that once, after the first trial.
        fingerprint_verified = False
        for position, (scenario_name, trial_index) in enumerate(schedule, 1):
            spec = by_name[scenario_name]
            print(
                f"── {spec.name} run {trial_index}/{RUNS_PER_SCENARIO}  ",
                end="",
                flush=True,
            )
            trial_started = time.perf_counter()
            trial_meta: dict[str, Any] = {}
            score, line, trace_completeness, harness_approvals = await _run_trial(
                client,
                jwt,
                oracle_client,
                fault_adapter,
                spec,
                creds,
                trial_meta,
            )
            _record_statistical_trial(
                spec,
                score,
                trial_index=trial_index,
                latency_seconds=time.perf_counter() - trial_started,
                trace_completeness=trace_completeness,
                harness_approvals=harness_approvals,
            )
            _record_confidence_observations(
                spec,
                score,
                trial_index=trial_index,
            )
            all_scores.append(score)
            print(line)
            for note in trial_meta.get("notes", ()):
                print(f"   {note}")
            if not fingerprint_verified:
                fingerprint_verified = True
                await _verify_declared_fingerprint(
                    client, jwt, trial_meta.get("incident_id"), creds
                )
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
