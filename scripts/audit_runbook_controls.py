#!/usr/bin/env python3
"""Negative controls for scripts/audit_runbook_coverage.py.

A pass count is worthless on its own. An earlier version of the coverage
grader reported 22/22 on the Meridian runbooks while being blind to a deleted
prohibition: one branch's blanket "do not restart, scale or patch" satisfied
the requirement document-wide, so removing the prohibition that actually
mattered changed nothing. The score was measuring the grader's leniency, not
the runbooks.

Each control here deletes exactly one property from a copy of the runbook set
and asserts the named scenario flips PASS -> FAIL. Run it after any change to
the grader or to the runbooks. Controls that go BLIND are the finding.

One control is expected to stay BLIND -- see KNOWN_BLIND below.

Usage:
    python scripts/audit_runbook_controls.py
    python scripts/audit_runbook_controls.py --runbooks runbooks/meridian
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_RUNBOOKS = REPO / "runbooks" / "meridian"
DEFAULT_CORPUS = REPO / "benchmarks" / "datasets" / "v2" / "runbook_corpus_snapshot.json"

# (label, file, regex to delete, scenario that must flip to FAIL)
CONTROLS = [
    (
        "remove the scale prohibition from the OOM remediation branch",
        "oom-memory.md",
        r"- \*\*Do NOT scale checkout-service\.\*\*.*?wrong action on this alert\.\n",
        "checkout_memory_leak_oom",
    ),
    (
        "remove the numeric threshold from the latency do-nothing branch",
        "high-latency.md",
        r" — checkout p95 below \*\*1\.5 s\*\*,\npayment p95 below \*\*1\.0 s\*\*,"
        r" inventory db p90 below \*\*1\.0 s\*\*",
        "inventory_subthreshold_slow_queries",
    ),
    (
        "remove every prescribed action from the bad-deploy branch",
        "high-error-rate.md",
        r"2\. `rollback_deployment.*?4\. If neither recovers within two verification passes, escalate\.\n",
        "bad_deploy_checkout",
    ),
    (
        "remove the scale prohibition from the erroring-dependency branch",
        "high-latency.md",
        r"- \*\*Do NOT scale checkout-service\.\*\* Unlike Branch C.*?worse\.\n",
        "payment_errors_cascade_to_checkout_latency",
    ),
    (
        "remove the recovery probe from the dependency runbook's verification",
        "downstream-dependency-failure.md",
        r"## Verification\n\n```\nmin\(payment_provider_up\{service=\"payment-service\"\}\)\n```\n",
        "payment_provider_outage",
    ),
]

# Deleting Branch E leaves Branch C, which satisfies bad_deploy_checkout's
# contract by letter: it prescribes restart (permitted) and forbids scale. The
# grader reads content, not routing -- it cannot know the decision procedure
# would never send a bad deploy to the provider-outage branch. Closing this
# would mean asserting a scenario-to-branch mapping that I would author myself,
# which makes the grader agree with me by construction. Routing is measured by
# running the agent, not by reading the page.
KNOWN_BLIND = {"remove every prescribed action from the bad-deploy branch"}


def verdicts(runbooks: Path, corpus: Path) -> dict[str, str]:
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "audit_runbook_coverage.py"),
            "--corpus",
            str(corpus),
            "--proposed",
            str(runbooks),
            "--format",
            "table",
        ],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    found = {}
    for line in proc.stdout.splitlines():
        match = re.match(r"^(\S+)\s+(PASS|FAIL)\s", line)
        if match:
            found[match.group(1)] = match.group(2)
    if not found:
        sys.exit(f"audit produced no verdicts:\n{proc.stdout}\n{proc.stderr}")
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runbooks", type=Path, default=DEFAULT_RUNBOOKS)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    args = parser.parse_args()

    baseline = verdicts(args.runbooks, args.corpus)
    passing = sum(1 for v in baseline.values() if v == "PASS")
    print(f"baseline: {passing}/{len(baseline)} scenarios pass\n")

    unexpected = 0
    for label, filename, pattern, scenario in CONTROLS:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "runbooks"
            shutil.copytree(args.runbooks, work)
            target = work / filename
            mutated, count = re.subn(
                pattern, "", target.read_text(), flags=re.DOTALL
            )
            if count == 0:
                print(f"STALE {label}\n      pattern matched nothing in {filename}")
                unexpected += 1
                continue
            target.write_text(mutated)

            before = baseline.get(scenario)
            after = verdicts(work, args.corpus).get(scenario)
            detected = before == "PASS" and after == "FAIL"
            expected_blind = label in KNOWN_BLIND
            if detected:
                status = "OK   "
            elif expected_blind:
                status = "KNOWN"
            else:
                status = "BLIND"
                unexpected += 1
            print(f"{status} {label}")
            print(f"      {scenario}: {before} -> {after}")

    print()
    if unexpected:
        print(f"grader is unexpectedly blind to {unexpected} of {len(CONTROLS)} removals")
        return 1
    print(
        f"all {len(CONTROLS) - len(KNOWN_BLIND)} controls detected "
        f"({len(KNOWN_BLIND)} known-blind, documented in KNOWN_BLIND)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
