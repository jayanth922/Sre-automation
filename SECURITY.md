# Security policy

## Reporting a vulnerability

Do not open a public issue containing credentials, tenant data, exploit
details, or an unpatched vulnerability. Contact the repository owner privately
with the affected revision, reproduction steps, impact, and any suggested
mitigation. A public advisory can be prepared after a fix is available.

## Credential hygiene

- Keep runtime secrets in ignored `.env` files, Kubernetes Secrets, or the
  encrypted tenant credential store.
- Never paste live tokens into documentation, fixtures, traces, screenshots,
  terminal captures, or issue descriptions.
- Run `bash scripts/ci/check_no_static_secrets.sh` before publishing changes.
- If a credential is committed, revoke it first. Deleting the current file does
  not remove the value from Git history; coordinate a history rewrite when the
  repository's exposure requires one.

## Operational boundaries

The executor is read-only unless live execution is explicitly enabled. Enabling
it does not bypass policy, tenant, namespace, approval, freshness, cluster-lock,
or idempotency checks at the mutation gateway. Treat changes to those checks as
security-sensitive and run the complete test and release-evidence gates.
