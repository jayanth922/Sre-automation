# Alembic Revision History

This folder contains the ordered schema revisions for the backend database. The filenames tell the story of how the platform data model evolved from the initial SaaS schema into an incident-response system with jobs, timeline events, audits, and cluster metadata.

## Revision Timeline

- [42d7600c0b2a_initial_saas_schema.py](42d7600c0b2a_initial_saas_schema.py) establishes the initial SaaS tables.
- [76b2c82e93f1_add_job_model.py](76b2c82e93f1_add_job_model.py) adds job support for queued work.
- [a1b2c3d4e5f6_add_cluster_infra_slo_audit.py](a1b2c3d4e5f6_add_cluster_infra_slo_audit.py) expands cluster metadata and introduces SLO and audit-related schema.
- [b3c4d5e6f7a8_add_incident_timeline_events.py](b3c4d5e6f7a8_add_incident_timeline_events.py) adds the incident timeline event table.
- [c4d5e6f7a8b9_add_pending_supervisor_to_incident_timeline_events.py](c4d5e6f7a8b9_add_pending_supervisor_to_incident_timeline_events.py) adds supervisor follow-up state to timeline events.
- [d6d22479d2ee_add_jobs_table.py](d6d22479d2ee_add_jobs_table.py) captures the jobs-table evolution that the runtime uses for cluster work items.

## What To Preserve

When adding a new revision:

1. Keep the filename ordered by Alembic revision dependencies.
2. Make sure downgrade steps are included unless the migration is intentionally irreversible.
3. Avoid reusing old revisions for new changes.
4. Verify that the generated migration still matches the live models in [../../models.py](../../models.py).

## Why This Folder Matters

Timeline events and job tracking are central to the product story. The dashboard, agent runtime, and follow-up behavior all depend on this schema history staying consistent, especially for incident transcripts and supervisor-pending state.