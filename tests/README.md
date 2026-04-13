# Tests

This folder contains the repository-level Python tests that validate the control-plane behavior: incident timeline persistence, supervisor follow-up routing, and transcript/summary generation.

These tests are intentionally focused on the product contracts that matter most to the dashboard and the agent runtime. They are not broad infrastructure tests; they are behavior checks for the incident loop.

## Test Coverage

- [test_timeline_crud.py](test_timeline_crud.py) checks incident timeline writes, pending-supervisor handling, and summary normalization.
- [test_supervisor_follow_up.py](test_supervisor_follow_up.py) checks the supervisor routing behavior for assistant-mode and follow-up flows.
- [test_mission_control_follow_up.py](test_mission_control_follow_up.py) covers mission-control and follow-up behavior across the incident lifecycle.

## How To Run

From the repository root:

```bash
pytest
```

For a narrower run:

```bash
pytest tests/test_timeline_crud.py
pytest tests/test_supervisor_follow_up.py
pytest tests/test_mission_control_follow_up.py
```

The Python test configuration in [pyproject.toml](../pyproject.toml) already points pytest at this folder.

## What These Tests Prove

- Timeline rows are written with the correct pending and handled state.
- The supervisor emits the expected follow-up routing decisions.
- Summary generation captures the evidence needed by the incident dashboard.

## How To Use This Folder

Run these tests when you want to validate changes to the incident timeline, the supervisor routing model, or the follow-up story that ties the agent runtime to the dashboard transcript. If those behaviors change, this is where you should add coverage first.

## Related Docs

- [../README.md](../README.md)
- [../backend/README.md](../backend/README.md)
- [../sre_agent/README.md](../sre_agent/README.md)