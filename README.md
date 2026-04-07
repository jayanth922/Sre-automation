# Multi-Agent SRE Assistant

An autonomous Site Reliability Engineering platform that investigates production incidents, plans remediations, and executes fixes — with human approval at every high-risk step.

Built as a capstone project for CMPE295A at San José State University.

---

## What it does

When an alert fires in a customer's infrastructure, the platform:

1. **Observes** — dispatches a swarm of AI agents to query Kubernetes, Prometheus, Loki, GitHub, and runbook databases in parallel
2. **Orients** — a reflector agent correlates findings and identifies root cause
3. **Decides** — a planner proposes a remediation and scores its risk
4. **Acts** — a policy gate checks safety rules; a human approves if the action is risky; the executor runs the fix
5. **Verifies** — confirms the issue is resolved and closes the incident

All tool execution (querying the customer's infra) happens by connecting directly to the Model Context Protocol (MCP) servers deployed alongside the customer infrastructure. All AI reasoning happens on the SaaS platform. No customer data leaves their environment unintentionally.
Customers register a cluster and provide their infrastructure endpoints (Prometheus, Loki, K8s API, GitHub repo). MCP tool servers on the platform connect directly to these endpoints — no agent to install, no infrastructure to manage on the customer side.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                    SaaS Platform (us)                        │
│                                                              │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │  sre-agent-api  (FastAPI + LangGraph)                   │ │
│  │                                                         │ │
│  │   Webhook ──► Supervisor ──► Investigation Swarm        │ │
│  │                              (K8s / Metrics / Logs      │ │
│  │                               GitHub / Runbooks)        │ │
│  │               Reflector ──► Policy Gate ──► Exec        │ │
│  │               Verify ──► Incident Closed                │ │
│  └─────────────────────────────────────────────────────────┘ │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │  MCP Tool Servers (edge stack via host ports)           │ │
│  │   mcp-k8s · mcp-prometheus · mcp-loki                   │ │
│  │   mcp-github · mcp-runbooks                             │ │
│  └─────────────────────────────────────────────────────────┘ │
│  ┌─────────────┐  ┌─────────┐  ┌────────┐  ┌──────────┐    │
│  │  PostgreSQL │  │  Redis  │  │ Qdrant │  │  Ollama  │    │
│  └─────────────┘  └─────────┘  └────────┘  └──────────┘    │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │  Dashboard (Next.js)  :3001                             │ │
│  │  Cluster overview · Incidents · Approvals · Audit       │ │
│  └─────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────┘
              ▲  Tool execution (HTTP/SSE)       ▼
              ▲  Tool results                    ▼
┌──────────────────────────────────────────────────────────────┐
│             Customer Edge (edge_mcp_servers/)                │
│                                                              │
│     mcp-k8s         (Kubernetes API)                         │
│     mcp-prometheus  (Metrics queries)                        │
│     mcp-loki        (Log queries)                            │
│     mcp-github      (Code + PRs)                             │
│     mcp-runbooks    (Runbooks)                               │
│                                                              │
│  Standalone MCP servers that expose infra data securely.     │
└──────────────────────────────────────────────────────────────┘
              ▲  Alertmanager webhook  ▼
              ▲  Direct API calls (customer-provided URLs)  ▼
┌──────────────────────────────────────────────────────────────┐
│              Customer Infrastructure (existing)              │
│   K8s Cluster · Prometheus · Loki · Alertmanager · GitHub    │
└──────────────────────────────────────────────────────────────┘
```

### Key design decisions

| Decision | Rationale |
|---|---|
| Tool execution on customer edge | Customer data never leaves their network |
| AI reasoning on SaaS platform | Customers don't need GPUs |
| Direct HTTP/SSE to MCP Servers | Radically simplifies deployment; no complex async job queues |
| Direct-connect architecture | No agent to install on customer side; simple URL-based integration |
| MCP tool servers on platform | Standardized tool protocol; each tool server is independently scalable |
| AI reasoning on SaaS platform | Customers don't need GPUs or LLM infrastructure |
| Human approval gate | High-risk PROD actions require explicit sign-off |
| Immutable audit trail | SOC2 compliance; every action logged |
| Cluster-scoped infra URLs | Each cluster stores its own Prometheus, Loki, K8s, GitHub endpoints |

---

## Repository structure

```
CMPE295A_Multi_Agent_SRE_Assistant/
│
├── backend/                  # Data layer (shared by sre_agent)
│   ├── models.py             # SQLAlchemy ORM: User, Org, Cluster, Incident, Job, SLO, AuditEvent
│   ├── crud.py               # All database operations
│   ├── auth.py               # JWT + password hashing
│   ├── schemas.py            # Pydantic request/response types
│   ├── rbac.py               # Role enforcement (admin / member)
│   ├── rate_limit.py         # Sliding-window rate limiter
│   └── routers/auth.py       # Login and registration endpoints
│
├── sre_agent/                # Agent system + API server
│   ├── agent_runtime.py      # FastAPI app; webhook receiver; job orchestration
│   ├── multi_agent_langgraph.py  # MCP client setup; agent system factory
│   ├── graph_builder.py      # LangGraph state machine (OODA loop)
│   ├── mcp_tool_wrapper.py   # Retry + circuit breaker for MCP tool calls
│   ├── policy_engine.py      # Deterministic safety rules
│   ├── agent_nodes.py        # Individual agent node implementations
│   ├── supervisor.py         # Routing logic between OODA phases
│   └── api/v1/
│       ├── auth_deps.py      # Shared JWT auth dependency
│       ├── clusters.py       # Cluster CRUD + lock/unlock
│       ├── incidents.py      # Incident list + manual trigger
│       ├── mission_control.py # Audit logs + human approval
│       ├── jobs.py           # Job queue management
│       └── slos.py           # SLO tracking + error budget
│
├── edge_mcp_servers/         # Customer-side MCP stack used by the SaaS platform
│   ├── docker-compose.yaml   # Active edge stack (5 MCP servers, Docker Compose only)
│   └── mcp_servers/
│       ├── k8s_real/         # kubectl-equivalent queries
│       ├── prometheus_real/  # PromQL execution
│       ├── loki_real/        # LogQL execution
│       ├── github_real/      # GitHub API (commits, PRs, issues)
│       └── runbooks_local/   # Local Markdown runbook lookup
│
├── platform/                 # SaaS infrastructure orchestration
│   ├── docker-compose.yaml   # Full stack: Postgres, Redis, Qdrant, Ollama, API, Dashboard
│   ├── Dockerfile            # SRE Agent API image
│   ├── Dockerfile.dashboard  # Next.js dashboard image
│   ├── start.sh
│   └── stop.sh
│
└── dashboard/                # Next.js frontend
    ├── app/(auth)/           # Login / register
    └── app/(dashboard)/      # Cluster overview, incidents, audit logs
```

---

## How the OODA loop works

```
Alert received  (Alertmanager webhook  ──►  /api/v1/alerts/webhook)
         │
         ▼
   [SUPERVISOR]   decides which phase to enter
         │
         ▼  OBSERVE
   [INVESTIGATION SWARM]  — runs in parallel
    ├─ K8s agent      pod status, events, restart counts
    ├─ Metrics agent  CPU, memory, error rate, latency (PromQL)
    ├─ Logs agent     error patterns, stack traces (LogQL)
    ├─ GitHub agent   recent commits, open PRs, deployments
    └─ Runbooks agent existing SOPs for this alert type
         │
         ▼  ORIENT
   [REFLECTOR]   correlates all findings → root cause hypothesis
         │
         ▼  DECIDE
   [POLICY GATE]
    ├─ risk score < threshold  →  auto-approved
    └─ risk score ≥ threshold  →  paused, dashboard notified
                                  human approves or rejects
         │
         ▼  ACT
   [EXECUTOR]   executes tool calls via MCP connection
   [EXECUTOR]   dispatches tool calls via MCP servers
         │
         ▼  VERIFY
   Metrics return to normal?  →  incident RESOLVED
                                 audit trail written
```

---

## Safety rules (policy engine)

The policy engine is evaluated before any action reaches the executor:

| Action | Environment | Behaviour |
|---|---|---|
| `RESTART` | PROD, risk score > 3.0 | Blocked |
| `DELETE` | PROD | Always blocked |
| `SCALE_DOWN` to 0 replicas | PROD | Blocked (causes outage) |
| `ROLLBACK` | PROD, no approval flag | Requires human approval |
| Any action | Non-PROD | Allowed |

---

## Data models

| Model | Purpose |
|---|---|
| `Organization` | Tenant; owns users and clusters |
| `User` | Email + hashed password + role (ADMIN / MEMBER) |
| `Cluster` | Customer cluster; stores infra URLs (Prometheus, Loki, K8s, GitHub, Notion) |
| `Incident` | One per alert; tracks status (OPEN → INVESTIGATING → RESOLVED) |
| `Job` | Unit of work for tool execution (type: `INVESTIGATE`) |
| `AuditEvent` | Immutable record of every remediation action |
| `SLO` | Service level objective with error budget tracking |

Duplicate incident detection: if an incident with the same title is already OPEN or INVESTIGATING within the last 30 minutes, a new one is not created.

---

## Quickstart

### 1. Configure environment

```bash
cp .env.example .env
# Edit .env — fill in GROQ_API_KEY, GITHUB_TOKEN, and customer infra URLs
```

### 2. Run the SaaS platform

```bash
cd platform
docker compose up -d --build
```

API is up at `http://localhost:8080`. Dashboard at `http://localhost:3002`.

Seed the database (creates admin user + sample org):

```bash
python -m backend.seed
```

### 2. Deploy the edge MCP servers (customer side)

```bash
cd edge_mcp_servers
cp .env.example .env            # fill in your infra URLs
docker compose up -d --build
```

Get a `CLUSTER_TOKEN` from the dashboard after creating a cluster, or via the API:
### 3. Register a cluster

Create a cluster with your customer's infrastructure endpoints:

```bash
curl -X POST http://localhost:8080/api/v1/clusters \
  -H "Authorization: Bearer <admin_jwt>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "prod-cluster",
    "prometheus_url": "http://host.docker.internal:9090",
    "loki_url": "http://host.docker.internal:3100",
    "github_repo": "org/repo",
    "github_token": "ghp_..."
  }'
```

Or use the dashboard — the cluster creation form collects all infrastructure endpoints.

### 4. Run the demo app (optional)

`SRE_Demo_App/` simulates a customer's infrastructure with flaky microservices. Alertmanager fires webhooks to the platform automatically.

```bash
cd /path/to/SRE_Demo_App
docker compose up -d --build
```

### 5. Trigger a test incident

```bash
curl -X POST http://localhost:8080/api/v1/clusters/<cluster_id>/trigger \
  -H "Authorization: Bearer <jwt>" \
  -H "Content-Type: application/json" \
  -d '{"title": "High error rate on checkout-service", "severity": "critical"}'
```

Watch the investigation in the dashboard under Incidents.

---

## Configuration

### Platform (`.env`)

| Variable | Description |
|---|---|
| `SECRET_KEY` | JWT signing key (`openssl rand -hex 32`) |
| `LLM_PROVIDER` | `groq` or `ollama` |
| `GROQ_API_KEY` | Groq API key (if using Groq) |
| `OLLAMA_BASE_URL` | Ollama endpoint (if using Ollama) |
| `OLLAMA_MODEL` | Ollama model ID, defaults to `gemma3:1b` |
| `OLLAMA_NUM_CTX` | Ollama context window size, defaults to `32768` |
| `SEED_ADMIN_EMAIL` | Admin user created on first run |
| `SEED_ADMIN_PASSWORD` | Admin password |

### Edge MCP Servers (`edge_mcp_servers/.env`)

| Variable | Description |
|---|---|
| `POSTGRES_USER` | PostgreSQL username |
| `POSTGRES_PASSWORD` | PostgreSQL password |
| `POSTGRES_DB` | PostgreSQL database name |
| `PROMETHEUS_URL` | Customer's Prometheus endpoint |
| `LOKI_URL` | Customer's Loki endpoint |
| `GITHUB_TOKEN` | GitHub personal access token |
| `GITHUB_REPO` | `org/repo` of the monitored codebase |
| `RUNBOOKS_DIR` | Local Markdown runbooks directory mounted into the edge runbooks server |
| `NOTION_API_KEY` | Notion integration token (optional) |
| `NOTION_DATABASE_ID` | Notion DB ID for runbooks (optional) |
| `K8S_API_SERVER` | Kubernetes API server URL (optional) |
| `K8S_TOKEN` | Kubernetes service account token (optional) |
| `DEBUG` | Enable debug logging (`true` / `false`) |

Infrastructure services (PostgreSQL, Redis, Qdrant, Ollama) are configured in `platform/docker-compose.yaml` with sensible defaults. The platform connects to the edge MCP stack over host ports `4000`-`4004`.

---

## API reference

All endpoints require `Authorization: Bearer <jwt>` unless noted.

### Auth
| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/v1/auth/register` | Rate limited: 3 req/min |
| `POST` | `/api/v1/auth/login` | Rate limited: 5 req/min |

### Clusters
| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/v1/clusters` | Create cluster with infra URLs |
| `GET` | `/api/v1/clusters` | List clusters for org |
| `DELETE` | `/api/v1/clusters/{id}` | Admin only |
| `POST` | `/api/v1/clusters/{id}/lock` | Emergency lock toggle (admin only) |
| `GET` | `/api/v1/clusters/{id}/audit` | Audit trail |

### Incidents
| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/v1/clusters/{id}/incidents` | List incidents |
| `POST` | `/api/v1/clusters/{id}/trigger` | Manually trigger investigation |
| `POST` | `/api/v1/alerts/webhook` | Alertmanager webhook (no auth) |

### Mission control
| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/v1/incidents/{id}/logs` | Audit + execution logs |
| `GET` | `/api/v1/incidents/{id}/status` | LangGraph execution state |
| `POST` | `/api/v1/incidents/{id}/approve` | Approve pending remediation |

### SLOs
| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/v1/clusters/{id}/slos` | Create SLO |
| `GET` | `/api/v1/clusters/{id}/slos` | List SLOs |
| `GET` | `/api/v1/clusters/{id}/slos/{slo_id}/status` | Error budget remaining |

### Edge integrations (authenticated by CLUSTER_TOKEN where applicable)
| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/v1/agent/heartbeat` | Liveness signal (30 req/min) |
| `POST` | `/api/v1/agent/telemetry` | Forward metrics/logs (60 req/min) |

---

## Tech stack

| Layer | Technology |
|---|---|
| Agent orchestration | LangGraph, LangChain |
| LLM | Groq (cloud) or Ollama (self-hosted) |
| Tool protocol | Model Context Protocol (MCP) via FastMCP |
| API server | FastAPI (async, Python 3.12) |
| Database | PostgreSQL + SQLAlchemy async + Alembic |
| Cache / state | Redis |
| Vector memory | Qdrant |
| Frontend | Next.js 14, TypeScript, Tailwind CSS |
| Auth | JWT (python-jose) + OAuth2PasswordBearer |
| Containers | Docker, Docker Compose |
| Observability | Prometheus + Loki + Alertmanager + Grafana |

---

## Demo app

`Target_Client/` (separate folder) simulates a customer's infrastructure with three intentionally flaky microservices. Alertmanager fires webhooks to the SaaS platform automatically. See `Target_Client/` for setup.
`SRE_Demo_App/` (separate repo) simulates a customer's infrastructure with three intentionally flaky microservices (api-gateway, checkout-service, inventory-service), a load generator, and a full observability stack (Prometheus, Loki, Alertmanager, Grafana). Alertmanager fires webhooks to the SaaS platform automatically. See `SRE_Demo_App/README.md` for setup.
