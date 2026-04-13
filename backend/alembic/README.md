# Alembic Migrations

This folder contains the database migration wiring for the backend schema. It is the source of truth for moving the platform database forward and for explaining how schema state is derived from the SQLAlchemy models.

## How It Works

- [env.py](env.py) imports [../models.py](../models.py) and binds `Base.metadata` to Alembic so autogeneration sees the current model state.
- [../database.py](../database.py) provides the database URL that Alembic uses in both offline and online migration modes.
- [script.py.mako](script.py.mako) is the standard Alembic revision template.
- [versions/](versions/) contains the ordered revision files.

The migration environment supports both offline and online runs. Online mode uses an async SQLAlchemy engine to execute migration steps against the live database.

## Common Commands

From the repository root or inside the platform container, the usual operations are:

```bash
alembic upgrade head
alembic downgrade -1
alembic revision --autogenerate -m "describe change"
```

The platform startup path already runs `alembic upgrade head` before launching the agent API.

## Operational Guidance

- Keep revision files small and ordered.
- Prefer new migrations over editing old ones once they have been applied in shared environments.
- Verify that the migration history still matches the model definitions in [../models.py](../models.py) after schema changes.

## Related Docs

- [../README.md](../README.md)
- [versions/README.md](versions/README.md)