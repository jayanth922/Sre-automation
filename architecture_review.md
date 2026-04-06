# System Architecture & AI SRE Agent Design Review

This document addresses the core design decisions, functional requirements, and the remaining roadmap for the Multi-Agent SRE Assistant project.

---

## 1. Functional Requirements for the AI SRE Agent
To be considered a true "AI Site Reliability Engineer", your LangGraph agent must successfully execute the **OODA Loop** (Observe, Orient, Decide, Act) lifecycle of an incident. Its functional requirements are:

1. **Ingestion & Triage:** Automatically receive and prioritize alerts from Prometheus Alertmanager.
2. **Telemetry Intelligence:** Autonomously translate natural language goals into precise PromQL queries to evaluate CPU, Memory, Latency, and Error margins.
3. **Log Diagnostics:** Formulate LogQL queries against Loki to isolate application-level exception stack traces during the exact incident time-window.
4. **Contextual Awareness:** Access repository code, architectural maps, and past incident histories to evaluate *why* an error is happening.
5. **Root Cause Analysis (RCA):** Synthesize the metrics, logs, and context into a definitive technical diagnosis.
6. **Remediation / Mitigation (Action):** (Optional but powerful) Propose or execute Kubernetes commands (e.g., `kubectl scale`, `kubectl rollout undo`) to stabilize the system.
7. **Post-Mortem Generation:** Automatically document the timeline, root cause, and fix in a structured format for the team.

---

## 2. Codebase Context, Runbooks, and Incident History
**You are absolutely correct.** Without historical context, the AI agent has amnesia. It will try to solve a recurring database timeout from scratch every single time, which is exactly what junior engineers do. A senior SRE checks past tickets. 

To elevate the agent from "junior" to "senior", we must implement a **Knowledge Base / RAG (Retrieval-Augmented Generation)** loop into the LangGraph flow:
* **Codebase Context:** The agent should have an MCP tool that allows it to read the source code of the failing microservice (e.g., if checkout is failing, the agent reads `checkout/app.py` to see what exceptions are thrown).
* **Runbooks:** Standard Operating Procedures (SOPs) stored as Markdown files. The agent searches: *"Is there a runbook for 'InventorySlowQueries'?"*
* **Incident History (Vector Database):** Every time the AI agent finishes an investigation, it should save the final Post-Mortem into a Vector Database. During a new incident, the agent performs a semantic search: *"Have we seen 'db_connection_refused' in the checkout service before? How did we fix it last month?"*

---

## 3. The Alertmanager Triggering Problem (Throttling & Debouncing)
Your intuition here is spot-on, and this is one of the hardest technical challenges in AIOps.

If a database goes down, you don't get *one* alert. You get 50 alerts cascading across every microservice. If your backend spins up a new LangGraph Agent workflow for every single HTTP POST from Alertmanager, you will instantly throttle your LLM API limits and suffer massive redundant API costs.

### The Solution: An Incident Aggregation Buffer
We cannot let Alertmanager trigger the LangGraph flow directly. We must insert a "Debouncer / Aggregator" layer:
1. **The Webhook Receiver:** A FastAPI endpoint receives the raw JSON from Alertmanager.
2. **The Buffer State:** Instead of triggering the AI, the webhook drops the alert into an active "Incident Context Pool" in Redis or a Database.
3. **The Debounce Timer:** The system waits for an "Alert Storm" to settle (e.g., wait 30-60 seconds for related alerts to arrive).
4. **The Trigger:** Once the timer expires, **ONE** single LangGraph Agent thread is spawned. It is handed the *entire* collection of grouped alerts, allowing it to see the full blast radius simultaneously rather than looking at 50 fragmented alerts.

---

## 4. Project Completion Status
You are in the **final overarching stretch** of the project. Here is where the architecture currently stands:

### ✅ Completed Milestones (The Environment)
* **The Target Environment:** The microservice sandbox is built and running in Kubernetes.
* **The Telemetry Stack:** Prometheus, Loki, Grafana, and Alertmanager are actively collecting data.
* **The Chaos Engine:** You have a deterministic, professional dashboard to trigger realistic production outages on demand.
* **The Agent Foundation:** The LangGraph state machine (`sre_agent/graph_builder.py`) and basic MCP tools exist.

### 🚧 Remaining Milestones (The "Last Mile" Integration)
To finish this master's project, we must connect the two worlds (The Kubernetes Sandbox to the LangGraph SRE Assistant). 
1. **Build the Wait/Debouncer Queue:** Create the webhook API that safely handles Alertmanager webhooks without flooding the LLM (Addressing Question #3).
2. **Implement RAG Context Storage:** Add a simple vector store or explicit file-read tools so the agent can look up previous incident runbooks (Addressing Question #2).
3. **The Ultimate End-to-End Test:** This is your final project defense. You will slide the Chaos Sliders, watch AlertManager fire, watch the Debouncer catch it, watch the LangGraph Agent execute its toolkit, and verify it correctly detects the root cause. 

We are essentially at the point of "wiring the brain to the nervous system."
