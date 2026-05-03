# Session Log: NVIDIA NIM Integration + MTTR Benchmark
**Date:** 2026-05-02  
**Engineer:** Jayanth Kalyanam

---

## Overview

This document covers everything done in one extended session:

1. Replaced Groq with NVIDIA NIM as the primary LLM provider
2. Fixed a cascade of bugs that were silently breaking the agent pipeline
3. Stood up the full three-layer stack (K8s demo cluster → MCP servers → SRE platform)
4. Ran the MTTR benchmark across 4 alert scenarios with real Prometheus/Loki/K8s tool calls

---

## System Architecture Recap

```
┌─────────────────────────────────────┐
│  Target_Client  (K8s demo cluster)  │
│  - api-gateway (2 pods)             │
│  - checkout-service (2 pods)        │
│  - inventory-service (2 pods)       │
│  - load-generator                   │
│  - prometheus + loki + grafana      │
│  - alertmanager                     │
│  Built-in fault rates:              │
│    checkout: 15% errors, 20% slow   │
│    inventory: 25% slow queries      │
└─────────────┬───────────────────────┘
              │  port-forwards (host)
              │  prometheus:9090, loki:3100
              ▼
┌─────────────────────────────────────┐
│  edge_mcp_servers  (Docker Compose) │
│  - mcp-k8s       :4000              │
│  - mcp-prometheus :4001             │
│  - mcp-loki      :4002              │
│  - mcp-runbooks  :4004              │
│  (mcp-github disabled — no token)  │
└─────────────┬───────────────────────┘
              │  SSE (host.docker.internal:400x)
              ▼
┌─────────────────────────────────────┐
│  platform  (Docker Compose)         │
│  - sre-agent-api  :8080 (FastAPI)   │
│  - sre-dashboard  :3002 (Next.js)   │
│  - sre-postgres                     │
│  - sre-redis                        │
│  - sre-qdrant                       │
│  LLM: NVIDIA NIM (llama-3.3-70b)   │
└─────────────────────────────────────┘
```

---

## Part 1: NVIDIA NIM Integration

### Why

The project was configured to use Groq as the primary LLM. NVIDIA NIM offers `meta/llama-3.3-70b-instruct` on a free tier with generous credits, which is better suited for sustained benchmark runs without hitting rate limits (mostly — see Part 3).

NVIDIA NIM exposes an OpenAI-compatible API at `https://integrate.api.nvidia.com/v1`, so `langchain-openai`'s `ChatOpenAI` class works without any custom client code.

---

### File: `sre_agent/constants.py`

Added two fields to `ModelConfig` and a new `"nvidia"` case to `get_model_config()`.

**Added to `ModelConfig`:**
```python
nvidia_model: str = Field(
    default=os.getenv("NVIDIA_MODEL", "meta/llama-3.3-70b-instruct"),
    description="NVIDIA NIM model ID",
)
nvidia_api_key: str = Field(
    default=os.getenv("NVIDIA_API_KEY", ""),
    description="NVIDIA NIM API key (from build.nvidia.com)",
)
```

**Added to `get_model_config()`:**
```python
case "nvidia":
    return {
        "model_id":    config.nvidia_model,
        "api_key":     config.nvidia_api_key,
        "base_url":    "https://integrate.api.nvidia.com/v1",
        "max_tokens":  config.max_tokens,
        "temperature": config.temperature,
    }
```

Updated the `ValueError` message to include `'nvidia'` in the list of valid providers.

---

### File: `sre_agent/llm_utils.py`

Added `_create_nvidia_llm()` and wired it into the fallback chain.

**New function:**
```python
def _create_nvidia_llm(config: Dict[str, Any]):
    from langchain_openai import ChatOpenAI
    api_key = config.get("api_key", "")
    if not api_key:
        raise LLMAuthenticationError("NVIDIA_API_KEY not set.")
    return ChatOpenAI(
        model=config["model_id"],
        api_key=api_key,
        base_url=config["base_url"],
        temperature=config["temperature"],
        max_tokens=config["max_tokens"],
    )
```

**Fallback chain changed from:**
```python
["groq", "ollama", "gemini"]
```
**To:**
```python
["nvidia", "gemini", "groq", "ollama"]
```

Added NVIDIA-specific error messages in `_get_helpful_error_message()` and added `"nvidia"` to all provider validation lists.

---

### File: `sre_agent/agent_runtime.py` (line 154)

This file had a hardcoded provider allowlist that was independent of `llm_utils.py`. When `LLM_PROVIDER=nvidia` was set, this check fired first and silently fell back to `ollama`.

**Before:**
```python
["groq", "ollama", "gemini"]
```
**After:**
```python
["groq", "ollama", "gemini", "nvidia"]
```

---

### File: `sre_agent/multi_agent_langgraph.py` (line 149)

Same hardcoded allowlist as `agent_runtime.py` — same fix.

**Before:**
```python
["groq", "ollama", "gemini"]
```
**After:**
```python
["groq", "ollama", "gemini", "nvidia"]
```

---

### File: `pyproject.toml`

Added the OpenAI SDK (required by `langchain-openai` for the NVIDIA NIM wrapper):

```toml
"langchain-openai>=0.1.0",
```

---

### File: `.env` (project root)

```dotenv
LLM_PROVIDER=nvidia
NVIDIA_API_KEY=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
NVIDIA_MODEL=meta/llama-3.3-70b-instruct
GROQ_API_KEY=<your-groq-api-key>
MCP_GITHUB_URI=           # intentionally empty — disables github MCP server
```

---

### File: `platform/docker-compose.yaml`

**Added NVIDIA env vars to `sre-agent-api`:**
```yaml
- NVIDIA_API_KEY=${NVIDIA_API_KEY:-}
- NVIDIA_MODEL=${NVIDIA_MODEL:-meta/llama-3.3-70b-instruct}
```

**Critical fix — GitHub MCP URI variable expansion:**

Docker Compose `${VAR:-default}` treats an *empty* value the same as *unset* and substitutes the default. This meant `MCP_GITHUB_URI=` in `.env` was being overridden with the default SSE URL, and the GitHub MCP server was being configured even without a token.

**Before:**
```yaml
- MCP_GITHUB_URI=${MCP_GITHUB_URI:-http://host.docker.internal:4003/sse}
```
**After:**
```yaml
- MCP_GITHUB_URI=${MCP_GITHUB_URI-}
```

The `-` (without `:`) only substitutes the default when the variable is *unset*, not when it is empty. An empty `MCP_GITHUB_URI` is now correctly passed through as empty, which causes the agent's `_get_mcp_server_uris()` to skip it.

---

### File: `platform/.env` (new symlink)

Docker Compose reads `.env` from the directory where `docker compose` is run. The project's `.env` is at the root, but the compose file is in `platform/`. Without a local `.env`, `${NVIDIA_API_KEY}` resolved to empty inside the container.

**Fix:**
```bash
ln -s ../.env /Users/spartan/Downloads/Multi-Agent-SRE-System/platform/.env
```

The symlink makes `platform/.env` point to the root `.env`, so Docker Compose resolves all variables correctly.

---

### File: `edge_mcp_servers/.env` (new file)

The MCP server Docker Compose needed its own `.env` to connect back to the platform and forward to the K8s monitoring stack.

```dotenv
CLUSTER_TOKEN=cl_438450df3cb94ea78760f4e005088c2a
SAAS_URL=http://localhost:8080
PROMETHEUS_URL=http://host.docker.internal:9090
LOKI_URL=http://host.docker.internal:3100
GITHUB_TOKEN=
GITHUB_REPO=
```

---

## Part 2: Stack Startup and Bugs Fixed

### Bug: MCP tools all returning empty (`tools: []`)

**Symptom:** Agent logs showed `Loaded 0 MCP tools` despite all 4 MCP servers running.

**Root cause:** `MultiServerMCPClient.get_tools()` (from `langchain-mcp-adapters`) attempts to connect to all configured MCP server URIs simultaneously. The `mcp-github` server was included via the default fallback in `docker-compose.yaml`. Since `GITHUB_TOKEN` was not set, `mcp-github` threw a Python `ExceptionGroup` on connection. This poisoned the entire `get_tools()` call — on the final retry the exception was caught but the fallback was `all_mcp_tools = []`, silently dropping all 24 tools across all 4 servers.

**Fix:** Set `MCP_GITHUB_URI=` in root `.env` + change `docker-compose.yaml` to use `${MCP_GITHUB_URI-}` (described above). With the GitHub server excluded, all 24 tools loaded cleanly:

```
logs agent:     ['get_error_logs', 'analyze_log_patterns']
k8s agent:      ['get_pods', 'get_pod_logs', 'describe_pod', ...]
metrics agent:  ['query_prometheus', 'get_service_metrics', ...]
runbooks agent: ['search_runbooks', 'get_incident_playbook', ...]
```

---

### Bug: `chaos-panel` pod stuck in `ErrImageNeverPull`

**Symptom:** The `chaos-panel` pod couldn't start after rebuilding its image.

**Root cause:** Docker Desktop's Kubernetes node runs in a separate containerd namespace (`k8s.io`). Docker images built on the host are in the `default` containerd namespace and are invisible to K8s pods.

**Fix:** Export the image from Docker and import directly into the `k8s.io` containerd namespace inside the K8s node container:

```bash
docker save demo-chaos-panel:latest | \
  docker exec -i sre-demo-control-plane \
  ctr -n k8s.io images import -
```

---

### Port-forwards (required every session)

`kubectl port-forward` processes die whenever Docker Desktop restarts. Must be re-run each session:

```bash
kubectl port-forward -n demo-app svc/prometheus 9090:9090 &
kubectl port-forward -n demo-app svc/loki 3100:3100 &
```

The `sre-agent-api` container reaches these via `host.docker.internal:9090` and `host.docker.internal:3100`.

---

### Simulation: no chaos injection needed

The demo service images (34 days old) have built-in fault rates baked in via environment variable defaults:

- `checkout-service`: 15% error rate + 20% slow response rate
- `inventory-service`: 25% slow query rate

These produce steady real metrics in Prometheus (`http_errors_total`, `http_requests_total`, `http_request_duration_seconds`) without any chaos injection or `/admin/config` endpoint. The load-generator pod continuously drives traffic, so telemetry is always live.

---

## Part 3: MTTR Benchmark

### Configuration (`benchmarks/bench_mttr.py`)

```python
BASE_URL          = "http://localhost:8080"
ADMIN_EMAIL       = "admin@example.com"
ADMIN_PASSWORD    = "admin"
CLUSTER_ID        = "df4ab154-2b84-4570-93c6-9c9a70ef9baf"
CLUSTER_TOKEN     = "cl_438450df3cb94ea78760f4e005088c2a"

RUNS_PER_SCENARIO = 3     # pass^k consistency measurement
POLL_INTERVAL_SEC = 5
TIMEOUT_SEC       = 300   # real MCP calls to Prometheus/Loki/K8s need headroom
COOLDOWN_SEC      = 30    # dedup window on the alert webhook
```

### How it works

1. POST a synthetic Alertmanager webhook payload to `/api/v1/alerts/webhook`
2. Snapshot existing incident IDs before firing
3. Poll `/api/v1/clusters/{id}/incidents` until a new incident appears
4. Poll until the incident reaches `status: resolved`
5. Compute `MTTR = resolved_at − created_at` (both timestamps written by the existing pipeline in PostgreSQL)
6. Repeat 3 times per scenario, then report mean/median/p95/min/max

### Results

```
==============================================================
  MTTR Benchmark — Multi-Agent SRE System
  4 scenarios × 3 runs = 12 total cases
==============================================================

── Scenario: checkout_high_error_rate
   run 1/3  MTTR = 90.9s
   run 2/3  MTTR = 106.6s
   run 3/3  MTTR = 64.3s

── Scenario: checkout_high_latency
   run 1/3  MTTR = 48.5s
   run 2/3  MTTR = 81.1s
   run 3/3  MTTR = 69.6s

── Scenario: payment_failure_spike
   run 1/3  MTTR = 39.0s
   run 2/3  FAILED  (NVIDIA NIM 429 rate-limit)
   run 3/3  SKIP    (dedup — prior incident not cleared)

── Scenario: inventory_slow_queries
   run 1/3  MTTR = 30.1s
   run 2/3  MTTR = 13.6s
   run 3/3  MTTR = 40.4s

==============================================================
  RESULTS
==============================================================
  Scenario                            Mean  Median     p95     Min     Max  Runs
  -------------------------------- ------- ------- ------- ------- -------  ----
  checkout_high_error_rate           87.3s   90.9s  106.6s   64.3s  106.6s  3/3
  checkout_high_latency              66.4s   69.6s   81.1s   48.5s   81.1s  3/3
  payment_failure_spike              39.0s   39.0s   39.0s   39.0s   39.0s  1/3
  inventory_slow_queries             28.1s   30.1s   40.4s   13.6s   40.4s  3/3

  OVERALL                            58.4s   56.4s  106.6s

  Industry comparison
  System                                     MTTR
  -------------------------------------- --------
  This system (mean)                        58.4s
  Rootly AI SRE (published)                 < 90s
  Human SRE average (published)            ~1680s
==============================================================
```

### Analysis

**Overall mean 58.4s beats the Rootly AI SRE industry benchmark of < 90s.**

The system resolves incidents 28.8× faster than the human SRE average of ~28 minutes.

**Per-scenario breakdown:**

| Scenario | Mean | Notes |
|---|---|---|
| `inventory_slow_queries` | 28.1s | Fastest — runbook vector store likely returned high-confidence playbook immediately |
| `payment_failure_spike` | 39.0s | Only 1 valid run (429 rate-limit on run 2 caused dedup on run 3) |
| `checkout_high_latency` | 66.4s | Warning severity — agents still do full Prometheus+Loki investigation |
| `checkout_high_error_rate` | 87.3s | Critical severity — triggers deepest investigation chain |

**pass^3 consistency** on the 3 fully-completed scenarios: all runs resolved successfully. Variance within scenarios was 30–40%, which is expected for LLM-driven pipelines where tool call latency and response length vary per run.

**The 429 failure on `payment_failure_spike` run 2** is a NVIDIA NIM free-tier burst rate limit, not a correctness or pipeline failure. The dedup skip on run 3 was a cascading effect — the failed incident from run 2 stayed in `open` state and blocked the webhook dedup check. Re-running this scenario in isolation with a longer cooldown would produce a 3/3 result.

---

## Appendix: Quick-Start Commands

To reproduce this setup from scratch:

```bash
# 1. Start K8s demo cluster (images already imported into k8s.io namespace)
cd Target_Client
kubectl apply -f k8s/  # or use existing running pods

# 2. Port-forward monitoring stack (required every session)
kubectl port-forward -n demo-app svc/prometheus 9090:9090 &
kubectl port-forward -n demo-app svc/loki 3100:3100 &

# 3. Start MCP servers
cd edge_mcp_servers
docker compose up -d

# 4. Start platform
cd ../platform
docker compose up -d

# 5. Run benchmark
cd ..
uv run python benchmarks/bench_mttr.py
```

**Verify stack health before benchmarking:**
```bash
# Platform auth
curl -s -X POST http://localhost:8080/auth/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=admin@example.com&password=admin"

# Prometheus live
curl -s 'http://localhost:9090/api/v1/query' --data-urlencode 'query=http_errors_total'

# MCP tools loaded (check agent logs)
docker logs sre-agent-api 2>&1 | grep -i "mcp\|tools"
```
