# SRE Dashboard

Next.js 14 frontend for the SRE platform. Provides real-time visibility into clusters, incidents, and the AI agent's investigation progress.

---

## Pages

| Route | Description |
|---|---|
| `/login` | Email + password login |
| `/register` | New account creation |
| `/` (dashboard) | Cluster overview — status, heartbeat, open incidents |
| `/clusters/[id]` | Legacy redirect to that cluster's incidents table |
| `/clusters/[id]/incidents` | Incident table for a cluster |
| `/clusters/[id]/incidents/[incidentId]` | Incident dashboard — chat, live status flags, and incident details |
| `/clusters/[id]/audit` | Full audit trail for all actions on this cluster |

---

## Key components

### `MissionControl.tsx`
The main dashboard view. Shows:
- All clusters with health status (ONLINE / OFFLINE / MAINTENANCE)
- Open incident count per cluster
- Emergency lock toggle (admin only)
- Quick-link to trigger a manual investigation

### `IncidentCommandCenter.tsx`
Per-incident view. Shows:
- Incident title, severity, current status
- Live investigation log streamed from the agent
- **Approve / Reject** button when the policy gate pauses for human sign-off
- Execution timeline (OBSERVE → ORIENT → DECIDE → ACT → VERIFY)

### `MetricSparklines.tsx`
Inline sparkline charts for key metrics (error rate, latency, pod restarts). Pulls from the SaaS API which proxies Prometheus data from the edge.

---

## Auth

Authentication uses JWT stored in `localStorage`. The Next.js middleware (`middleware.ts`) redirects unauthenticated requests to `/login`.

The API base URL is configured via `NEXT_PUBLIC_API_URL` (defaults to `http://localhost:8080`).

---

## Development

```bash
cd dashboard
npm install
npm run dev         # starts on http://localhost:3000
```

Point it at a running API:

```bash
NEXT_PUBLIC_API_URL=http://localhost:8080 npm run dev
```

---

## Production build

```bash
npm run build
npm start
```

Or via Docker (from the platform `docker-compose.yaml`):

```bash
cd platform
docker compose up dashboard
```

---

## Environment variables

| Variable | Description |
|---|---|
| `NEXT_PUBLIC_API_URL` | SaaS API base URL (default: `http://localhost:8080`) |
