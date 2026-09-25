# Backend package

`backend` is Sentinel's relational system-of-record layer. It owns tenant,
identity, cluster, incident, job, approval, audit, timeline, SLO, and run
manifest persistence.

## Main modules

- [`models.py`](models.py): SQLAlchemy entities and enums.
- [`schemas.py`](schemas.py): request and response contracts.
- [`crud.py`](crud.py): tenant-scoped persistence operations.
- [`database.py`](database.py): async and synchronous session factories.
- [`auth.py`](auth.py): password hashing and token helpers.
- [`routers/auth.py`](routers/auth.py): first-run claim, login, refresh, logout,
  and account endpoints.
- [`alembic/`](alembic/): migration environment and ordered revisions.

There is no seeded administrator or demo cluster. The first registration on an
empty installation claims it and creates the first organization administrator;
later membership uses the invitation flow. Guardrails prevent removal of the
last active administrator.

## Database changes

Model and schema changes normally require a CRUD update, an Alembic revision,
and route/test updates. Apply migrations from the repository root:

```bash
uv run alembic upgrade head
```

The Alembic configuration points at `src/backend/alembic` and imports the
`backend` package through the repository's `src` layout.

## Boundaries

The backend stores authoritative state; it does not decide whether a mutation
is safe. Freshness, tenant and namespace scope, approvals, cluster locks, and
idempotency are enforced by the agent runtime immediately before execution.

Related documentation:

- [Agent runtime](../sre_agent/README.md)
- [Data model diagram](../../docs/architecture/images/backend-data-model.svg)
- [Local runtime](../../infra/local/README.md)
