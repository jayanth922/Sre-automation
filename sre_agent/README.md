# SRE Agent System

The AI brain of the platform. Receives alerts, runs multi-agent investigations, plans remediations, and dispatches tool calls via MCP servers to customer infrastructure.

---

## Entry point

`agent_runtime.py` is the FastAPI application. It:
- Exposes all `/api/v1/` endpoints
- Receives Alertmanager webhooks
- Manages incident lifecycle and job orchestration
- Hosts the human approval flow

Start it directly (outside Docker) for development:

```bash
uvicorn sre_agent.agent_runtime:app --reload --port 8080
```

---

## Agent graph (OODA loop)

Built in `graph_builder.py` using LangGraph. The graph is a directed state machine:

```
            ┌─────────────┐
 alert ────►│  SUPERVISOR │
            └──────┬──────┘
                   │  route
         ┌─────────┼─────────┐
         ▼         ▼         ▼
   [OBSERVE]  [ORIENT]  [DECIDE]  [ACT]
         │
         ▼
  Investigation Swarm (parallel)
   ├─ k8s_investigator
   ├─ metrics_investigator
   ├─ logs_investigator
   ├─ github_investigator
   └─ runbooks_investigator
         │
         ▼
   [REFLECTOR]  — root cause analysis
         │
         ▼
   [POLICY GATE]  — safety check
    ├─ approved ────► [EXECUTOR] ──► [VERIFY]
    └─ needs approval ──► pause, notify dashboard
                          human resumes via /approve
```

State is a `TypedDict` (`agent_state.py`) that flows through every node. Each node reads from state, calls tools or LLM, and writes results back.

---

## Key files

### `multi_agent_langgraph.py`
Creates the MCP client that connects to all tool servers (MCP servers running on the platform). Handles:
- Tool discovery (loads all tools from all MCP servers at startup)
- Retry with exponential backoff + jitter on connection failures
- Rate limit handling (429 responses)
- Wraps every tool with `mcp_tool_wrapper.py`

### `mcp_tool_wrapper.py`
Wraps each MCP tool with:
- **Retry logic** (tenacity): up to 3 attempts, exponential backoff
- **Circuit breaker**: stops calling a tool after 5 consecutive failures
- **Audit logging**: every tool call is written to `AuditEvent` in PostgreSQL

### `policy_engine.py`
Pure function — no LLM, no side effects. Takes a remediation plan and returns `(approved: bool, reason: str)`.

Rules (in priority order):
1. DELETE on PROD → always blocked
2. SCALE_DOWN to 0 on PROD → blocked
3. ROLLBACK on PROD without approval flag → blocked
4. RESTART on PROD with risk score > 3.0 → blocked
5. Everything else → allowed

### `agent_nodes.py`
One function per LangGraph node. Each node:
- Receives the full `AgentState`
- Calls tools or LLM as needed
- Returns a partial state update (only changed fields)

---

## API endpoints

All routes are mounted in `agent_runtime.py`:

| Router | Prefix | File |
|---|---|---|
| Auth | `/api/v1/auth` | `backend/routers/auth.py` |
| Clusters | `/api/v1` | `api/v1/clusters.py` |
| Incidents | `/api/v1` | `api/v1/incidents.py` |
| Mission Control | `/api/v1` | `api/v1/mission_control.py` |
| Jobs | `/api/v1` | `api/v1/jobs.py` |
| SLOs | `/api/v1` | `api/v1/slos.py` |

Shared auth dependency: `api/v1/auth_deps.py` — JWT validation + user lookup, imported by all protected routers.

---

## LLM configuration

Configured via `LLM_PROVIDER` env var:

- `groq` — uses Groq API (fast, cloud-hosted). Requires `GROQ_API_KEY`.
- `ollama` — uses local Ollama. Requires `OLLAMA_BASE_URL`. Default model: `llama3.2`.

`llm_utils.py` initialises the correct provider and returns a LangChain-compatible LLM object.

---

## State persistence

- **During investigation**: LangGraph `MemorySaver` (in-memory checkpointer) holds state per session
- **Pending approvals**: stored in Redis with 1-hour TTL; keyed by `session_id`
- **Completed incidents**: full summary written to PostgreSQL `incidents.result`
- **Audit events**: every tool call written to `AuditEvent` table (immutable)

---

## Adding a new agent node

1. Add a function in `agent_nodes.py`:
   ```python
   async def my_new_node(state: AgentState) -> dict:
       # read from state, call tools, return updates
       return {"my_field": result}
   ```

2. Register it in `graph_builder.py`:
   ```python
   graph.add_node("my_new_node", my_new_node)
   graph.add_edge("reflector", "my_new_node")
   ```

3. Add the field to `AgentState` in `agent_state.py` if you need new state.

---

## Adding a new safety rule

In `policy_engine.py`, add a condition to `evaluate_plan()`:

```python
if action.type == "MY_ACTION" and environment == "PROD":
    return False, "MY_ACTION blocked on PROD"
```

No LLM involved — the policy engine is deterministic.
