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
   - Grafana/Prometheus, with `min(payment_provider_up)` graphed;
   - a terminal on the Codespace.
4. `set -a; . /home/vscode/bench.env; set +a`, keys only. Never echo values on
   screen.
5. Rehearse once the day before (price below). If the rehearsal fails, demo
   from its recorded trace instead of a live run.

## Act 1: a real outage, fixed with approval (about 8 min)

```bash
BENCH_SCENARIOS=payment_provider_outage BENCH_RUNS_PER_SCENARIO=1 \
BENCH_DATASET_VERSION=v3 BENCH_DATASET_SPLIT=holdout BENCH_FAULT_MODE=automatic \
  .venv/bin/python evals/benchmarks/sre_bench.py
```

Leave `BENCH_AUTO_APPROVE` unset. The approval must be a human clicking in
Slack.

| Beat | Show | Say |
|---|---|---|
| Fault | the graph drops to 0 | "The payment provider is down; checkout fails as a cascade." |
| Alert | the incident appears in the console; a Slack thread opens | "Alertmanager's webhook opened the incident. No human paged the agent." |
| Investigation | the console timeline: specialists' tool calls (metrics, logs, k8s) | "Every claim it makes is tied to a tool call it actually made. The transcripts are stored content-addressed." |
| Policy gate | the proposed action and its risk class | "This is a critical-risk service. The policy allows restart, rollback or escalate and forbids scaling, so the agent must stop and ask." |
| Approval | approve in the Slack thread | "Slack resolves my profile email to a platform user and checks my role. It is the same gate the API uses, with no second path." |
| Recovery | the probe needs 2 consecutive passes; the graph back at 1 | "It is resolved only when the metric says so, not when the agent says so." |
| Trace | the run trace (spans, tokens, cost) | "Here is what that incident cost." Read the number off the trace. |

## Act 2: the right answer is no action (about 4 min)

Same command with `BENCH_SCENARIOS=payment_subthreshold_charge_errors`. The
policy forbids every mutating action here.

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

## Cost

Act 1 is about $1.00–1.50 per trial (past `payment_provider_outage` trials cost
$0.65–1.00; recent trials $0.95–1.40). Act 2 costs about the same. Rehearsal
plus live demo with both acts: about $4–6. Act 3 is free.
