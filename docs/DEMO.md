# Demo script

About 15 minutes, live, on the Codespace. One real incident fixed end to end
with a human approval, then one incident where the right answer is to do
nothing. Everything shown comes from the running system; nothing is
pre-recorded.

## Before the audience arrives (about 20 min)

1. `bash scripts/dev/codespace_boot.sh`: it must exit 0 with k3s healthy, the
   Alertmanager webhook wired and every pod Running.
2. Wait at least 5 minutes after the boot before injecting a fault.
3. Open these tabs:
   - console: port 3002, forwarded;
   - the Slack incident channel;
   - Grafana/Prometheus, with the checkout error ratio graphed:
     `sum(rate(http_errors_total{service="checkout-service"}[5m])) / clamp_min(sum(rate(http_requests_total{service="checkout-service"}[5m])), 1)`;
   - a terminal on the Codespace.
4. `set -a; . /home/vscode/bench.env; set +a`, keys only. Never echo values on
   screen.
5. Rehearse once the day before (price below). If the rehearsal fails, demo
   from its recorded trace instead of a live run.

## Act 1: a bad deploy, rolled back with approval (about 8 min)

The fault is a real rollout: the bench changes checkout-service's
`ERROR_RATE` env from 0 to 0.6 with `kubectl set env`, which creates a new
Deployment revision, the way a bad config deploy lands. A restart keeps the
env, so it does not fix anything. Only rolling back to the previous revision
does, and that is what the runbook's service-side-regression branch says to
do (`examples/meridian/runbooks/high-error-rate.md`, Branch E).

```bash
BENCH_SCENARIOS=checkout_bad_config_deploy BENCH_RUNS_PER_SCENARIO=1 \
BENCH_INCIDENT_TIMEOUT_SEC=2700 BENCH_AWAIT_HUMAN_APPROVAL=1 \
BENCH_DATASET_VERSION=v3 BENCH_DATASET_SPLIT=dev BENCH_FAULT_MODE=automatic \
  .venv/bin/python evals/benchmarks/sre_bench.py
```

Leave `BENCH_AUTO_APPROVE` unset. The approval must be a human clicking in
Slack, and `BENCH_AWAIT_HUMAN_APPROVAL=1` keeps the trial running while the
request waits; without it the bench ends the trial 30 seconds after the request
posts, restores the fault itself and closes the incident (rehearsal 2). Ignore
a request left over from such a trial: the platform should refuse it because
its incident is resolved (untested live), and if it ran, `rollout undo` from
the restored revision would land on the faulty one. Approve within 30 minutes; the
request expires after that (`APPROVAL_TTL_MINUTES`).

Before you start, `kubectl -n meridian rollout history deploy/checkout-service`
must show the healthy revision as the newest one and the env must read
`ERROR_RATE=0`; the bench refuses to inject otherwise.

The bench holds the alert until its probe reads above 0.45, as the real rule
would: about 3–4 minutes after the rollout, while the five-minute rate fills
(`BENCH_FAULT_VISIBLE_SEC`, default 360). Posting it earlier lets the agent's
first probe read the fault as sub-threshold and close the incident with no
action, which is what the first rehearsal did.

| Beat | Show | Say |
|---|---|---|
| Fault | `kubectl -n meridian rollout history deploy/checkout-service`: a new revision; then the graph climbing past 0.45 | "Someone just shipped a config change to checkout. Six in ten checkouts now fail." |
| Alert | the incident appears in the console; a Slack thread opens | "The error ratio crossed 0.45 and the alert landed on the platform's Alertmanager webhook. No human paged the agent." |
| Investigation | the console timeline: specialists' tool calls (metrics, logs, k8s) | "It rules things out in the runbook's order: the provider is up, the database is fine, and checkout rolled out a new revision at the moment errors started." |
| Policy gate | the proposed rollback and its risk class | "The runbook says roll back, never scale. This is a critical-risk service, so the agent must stop and ask." |
| Approval | approve in the Slack thread | "Slack resolves my profile email to a platform user and checks my role. It is the same gate the API uses, with no second path." |
| Recovery | rollout history shows the old template back; the probe needs 2 consecutive readings below 0.45 | "It is resolved only when the metric says so, not when the agent says so." |
| Trace | the run trace (spans, tokens, cost) | "Here is what that incident cost." Read the number off the trace. |

When the trial ends, the bench restores the env (a no-op if the rollback
already did) and then keeps closing any incident the fault caused for up to 7
minutes (`BENCH_QUIESCE_SEC`). Do not stop it during that window; that is the
guard that stops aftermath alerts from spending money.

## Act 2: the right answer is no action (about 4 min)

```bash
BENCH_SCENARIOS=payment_subthreshold_charge_errors BENCH_RUNS_PER_SCENARIO=1 \
BENCH_ALLOW_HOLDOUT=1 BENCH_INCIDENT_TIMEOUT_SEC=2700 \
BENCH_DATASET_VERSION=v3 BENCH_DATASET_SPLIT=holdout BENCH_FAULT_MODE=automatic \
  .venv/bin/python evals/benchmarks/sre_bench.py
```

The policy forbids every mutating action here.

Say: "There are a few charge errors, but below threshold. A good on-call
engineer writes it down and goes back to bed. The agent rates it SEV4 and takes
no action; if it tried to restart, the gate would refuse." If you're short on
time, show Run 3–9's recorded negative-control trace instead of a live run.

## Act 3: engineering rigour (about 3 min, no live calls)

- The fail-closed grader: `evals/benchmarks/structured_grading.py`. A run
  passes only if every criterion passes.
- Why two criteria read `REQUIRES_CALIBRATION`, and the plan to calibrate a
  judge against human labels:
  `evals/benchmarks/graders/CALIBRATION_DESIGN.md`. The blinded 25-case review
  set already exists.
- The test suite: `pytest -q` gives 2621 passed and 6 skipped.

## Do not claim

- No MTTR, accuracy or benchmark-score numbers. Every run so far is a
  single-scenario smoke run; the statistical campaigns are
  `NOT_DEMONSTRATED`.
- No "LLM-as-judge validated". The judge is designed, not built or calibrated.
- No "production-ready". Say instead: "a production-shaped system, running on
  a k3s lab cluster with a simulated payment stack."

Fine to say: per-incident cost and time read off the trace you just showed,
labelled "this run".

## If something breaks live

- The agent stalls: show the timeline up to that point. Fail-closed means it
  stops rather than guesses.
- Slack is down: approve through the console/API gate. It is the same gate.
- The fault doesn't inject: the bench cleans up and exits. Fall back to the
  rehearsal's trace.
- The trial ends and checkout is still failing: check `rollout history`. If
  something restarted checkout before the rollback, the restart made a new
  revision and `rollout undo` went back to the faulty one. Run
  `kubectl -n meridian set env deploy/checkout-service ERROR_RATE=0`.

## Cost

The 2026-10-18 Act 1 rehearsals cost $1.19 (stopped before a plan: the early
alert above) and $1.37 (a correct rollback plan, then the bench ended the trial
at the approval request; $0.03 of that was an aftermath incident the quiesce
step closed). A run that also executes and verifies the rollback should land at
about $1.50–2.00. Waiting for the approval click costs nothing: the agent is
paused. The 2026-10-17 rehearsal's total reached $2.44 only because a cascade
incident kept investigating after the trial ended. Act 2 costs about the same
as Act 1. Rehearsal plus live demo with both acts: about $4–6. Act 3 is free.
