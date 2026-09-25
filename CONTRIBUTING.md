# Contributing

Keep changes bounded, evidence-backed, and explicit about operational risk.

## Setup

```bash
uv sync --frozen --extra dev --extra temporal --extra anthropic
cd apps/dashboard && npm ci
```

## Before submitting a change

```bash
bash scripts/ci/check_python_quality.sh
bash scripts/ci/check_no_static_secrets.sh
uv run pytest -q
cd apps/dashboard && npm run lint && npx tsc --noEmit && npm run build
```

Run Helm, Kustomize, Terraform, and container checks when the change touches
deployment artifacts. Prompt, model-routing, evaluator, or tool-contract
changes must also satisfy the content-addressed release-evidence policy under
`evals/benchmarks/release/`.

Do not include paid benchmark runs unless their budget was explicitly approved.
Record important limitations and negative results alongside positive results.

## Pull requests

Explain the user-visible outcome, important invariants, verification performed,
and any remaining risk. Keep generated output, local environments, credentials,
and transient reports out of commits.
