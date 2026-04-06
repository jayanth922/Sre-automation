# Bottom-Up Validation and Build Strategy

Since the true reliability of the system is unknown, diving straight into complex architectural migrations (like moving to Kubernetes) or running complex multi-agent workflows is risky. If a foundational layer is broken, debugging a high-level agent failure becomes nearly impossible.

This plan proposes a strict **Layered, Bottom-Up Verification** approach. We will treat the system like a pyramid, starting at the base (Layer 0) and refusing to move upward until the current layer is proven to function 100% correctly.

## Proposed Verification Layers

### Layer 0: System Under Observation (The Target Client)
**Goal:** Prove the simulated e-commerce microservices and load generator actually function, and build a UI to control the chaos.

> **Note on "Real" vs "Simulated":** We do *not* need a fully functioning UI for the e-commerce store (like a React shopping cart). For SRE use cases, the APIs simulating the latency, throughput, and error rates of a real site are sufficient. However, we absolutely need a UI to *control* the chaos!

*   **Action 1:** Launch *only* the application services (`api-gateway`, `checkout-service`, `inventory-service`) and the `load-generator` via Docker Compose.
*   **Action 2:** Validate the APIs manually (Layer 0 tests) in `Target_Client/testing/` to ensure they return 200s, 500s or timeouts based on parameters.
*   **Action 3 (NEW):** Build a **Chaos Control Panel UI**. This will be a lightweight frontend (e.g., Streamlit or a simple HTML/JS page) attached to the load generator! It will have knobs, dials, and switches (e.g., "Trigger Burst", "Set Checkout Error Rate to 50%", "Enable Chaos Mode") so you can visually click a button and watch the SRE agent react.

### Layer 1: Observability and Telemetry (The Monitors)
**Goal:** Prove that the system's vital signs are being successfully recorded and analyzed.
*   **Action 1:** Launch Prometheus, Alertmanager, Loki, and Promtail.
*   **Action 2:** Open Prometheus UI. Go to `Status -> Targets` and verify all services are UP.
*   **Action 3:** Open Grafana UI. Verify dashboards display live data and test PromQL queries.
*   **Action 4:** Trigger the Chaos via the new UI designed in Layer 0, and verify Alertmanager successfully fires an alert webhook.

### Layer 2: Edge MCP Tooling (The Actuators)
**Goal:** Prove that Model Context Protocol servers can successfully expose secure tools for an LLM to use.
*   **Action 1:** *Architectural Decision Point.* Are we migrating to K8s?
*   **Action 2:** Launch the MCP Servers (`mcp-k8s`, `mcp-prometheus`, etc.).
*   **Action 3:** Run basic Python test scripts in `edge_mcp_servers/testing/` to hit the tool interfaces and verify data returns.

### Layer 3: SaaS Agent Core (The Brain)
**Goal:** Prove the LangGraph reasoning loop works, manages user data, and discovers tools.
*   **Action 1:** Write isolation tests in `sre_agent/testing/` to validate memory, tool discovery, and the router execution.

### Layer 4: Dashboard and Workflows (The UI / Delivery)
**Goal:** Prove the human operator can interact with the system securely.
*   **Action 1:** Launch the Streamlit / React Dashboard.
*   **Action 2:** Validate end-to-end connectivity: Human types in Dashboard -> SaaS Server updates DB -> Agent calculates thought -> SaaS streams Response -> Dashboard displays markdown.
