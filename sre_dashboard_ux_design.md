# SRE Agent Dashboard: UI/UX & Product Design Reference

This document outlines the core design principles and UI layout expectations for the human-facing interface of the Multi-Agent SRE Assistant.

---

## The Core Product Philosophy
**If the UI is just a blank ChatGPT-style text box, nobody will trust it in a production crisis.**

When an SRE engineer is stressed out during a 3:00 AM outage, they do NOT want to read walls of GenAI text. They want structure, hard evidence, and clear action buttons. The dashboard must be a surgical tool where the AI presents its evidence, offers a diagnosis, and asks the human for permission to act.

---

## 1. The 3 Core Components of the AI Dashboard

### A. The "Active Incidents" Feed (Left Sidebar)
Instead of a normal chat history, this is an incident queue. 
When Prometheus fires an alert, a new Incident Session is automatically created here.
* **Interactions:** The human can click an active incident (e.g., `#INC-042: Inventory High Latency`). 
* **Indicators:** A badge next to the incident should show the AI's current state: `[Investigating...]`, `[Root Cause Found]`, or `[Awaiting Human Approval]`.

### B. The "Investigation Timeline" (Main Center View)
This is the heart of the project. Instead of just a conversational chat, the UI should render the LangGraph agent's thoughts and actions as a chronological timeline of **evidence**.
* **10:00 AM** 🚨 Alert Received (`InventorySlowQueries`).
* **10:01 AM** 🔍 AI ran PromQL: `sum(irate(http_request_duration_seconds[1m]))`. *(UI shows a small, parsed snippet of the metric result).*
* **10:02 AM** 🔍 AI ran LogQL in Loki. *(UI shows a summarized finding: "Found 14 instances of `db_connection_refused`").*
* **10:03 AM** 🧠 **Diagnosis:** The AI outputs a concise, structured card:
  1. **Root Cause:** Inventory Database connection pool exhausted.
  2. **Confidence Score:** 95%.
  3. **Blast Radius:** Affecting Checkout and API Gateway.
* **10:04 AM** 🛠️ **Remediation Proposal:** The AI proposes a fix (e.g., scale the `inventory-service` replicas). A large **[APPROVE RUN]** or **[REJECT]** button is presented to the human.

### C. The Co-Pilot Chat (Bottom or Right Panel)
A standard text box where the human can converse with the agent during the investigation via natural language.
* **Interactions:** The human can interject to guide the AI: 
  * *"Can you also check if the CPU is spiking on the checkout pod?"* 
  * *"Please draft a post-mortem Markdown file for this and save it to the wiki."*

---

## 2. What to AVOID (Preventing Overcomplication)

To make this dashboard clean, effective, and enterprise-ready, aggressively avoid these "feature traps":

❌ **Do NOT rebuild Grafana:** 
Avoid trying to embed live, moving graphs everywhere. Grafana already exists for that. If the AI pulls a metric, just show a static snapshot or a simple summary of the metric value. The AI's job is **synthesis**, not visualization.

❌ **Do NOT dump raw logs into the UI:** 
If the AI finds 500 error logs, do not print them on the screen. The AI should summarize them (*"Found 500 Database Timeouts"*). Only provide an expandable `[View Log Snippet]` button if the human intentionally wants to physically verify the exact stack trace.

❌ **Do NOT allow Auto-Execution (The "Skynet" Problem):** 
**Never** let the AI execute a modifying Kubernetes command (like restarting a pod or scaling a deployment) without human approval. The dashboard must strictly enforce **"Human-in-the-Loop"**. The AI does the heavy lifting of the *investigation*; the human makes the final *decision*.
