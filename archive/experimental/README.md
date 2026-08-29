# Experimental archive

Modules here were removed from the live Sentinel product path because they had
tests and documentation but **no production entry point** (no FastAPI route,
graph node, or dashboard affordance).

| Module | Former path | Reason |
|--------|-------------|--------|
| `generative_course.py` | `sre_agent/generative_course.py` | Learning-course generator never mounted on an API or UI |

Do not import these from `sre_agent.agent_runtime`, `graph_builder`, or dashboard
code. Reintroduce only behind an explicit route + owner + tests.
