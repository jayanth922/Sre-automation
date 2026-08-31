// Shared types + helpers for the Sentinel console.
// Types mirror backend/schemas.py exactly so the UI renders real data.

export interface Cluster {
  id: string
  name: string
  status: "online" | "degraded" | "stale" | "offline" | "maintenance"
  last_heartbeat: string | null
  heartbeat_source?: string | null
  heartbeat_reason?: string | null
  age_seconds?: number | null
  created_at: string
  prometheus_url: string | null
  loki_url: string | null
  k8s_api_server: string | null
  github_repo: string | null
  github_app_installation_id?: string | null
  notion_database_id: string | null
  jira_url: string | null
  jira_email: string | null
  jira_project_key: string | null
  metrics_config: string | null
  namespace: string | null
  llm_provider: string | null
  llm_model: string | null
  llm_base_url: string | null
}

export type Severity = "critical" | "high" | "medium" | "low"
export type IncidentStatusT = "open" | "investigating" | "resolved"

export interface Incident {
  id: string
  cluster_id: string
  title: string
  description: string | null
  severity: Severity
  status: IncidentStatusT
  summary: string | null
  created_at: string
  resolved_at: string | null
  jira_issue_key: string | null
}

export interface TimelineEvent {
  id: string
  incident_id: string
  sequence: number
  event_type: string
  speaker_role: string
  title: string | null
  content: string
  payload: Record<string, unknown> | null
  pending_supervisor: boolean
  handled_at: string | null
  created_at: string
}

export interface Transcript {
  incident: Incident
  conversation_mode: "investigation" | "assistant"
  summary: string | null
  events: TimelineEvent[]
}

export interface SLO {
  id: string
  cluster_id: string
  name: string
  sli_metric: string
  target: number
  window_days: number
  current_value: number | null
  error_budget_remaining: number | null
  last_calculated: string | null
}

export interface SLOStatus {
  slo: SLO
  budget_consumed_percent: number
  burn_rate_1h: number | null
  burn_rate_6h: number | null
  is_breaching: boolean
}

export interface MetricsSnapshot {
  latency: number | null
  errors: number | null
  cpu: number | null
  mem: number | null
}

export interface Analytics {
  weekly_incidents: { week: string; count: number }[]
  severity_distribution: { severity: string; count: number }[]
  stats: {
    total_incidents: number
    resolved: number
    resolution_rate_pct: number
    mttr_minutes: number
  }
  top_alerts: { title: string; count: number }[]
  cluster_name: string
}

export interface Recommendations {
  cluster_name: string
  recommendations: string
  stats: { total: number; mttr_minutes: number; resolution_rate: number }
  generated_at: string
}

export interface NLQueryResult {
  question: string
  promql: string
  valid: boolean
  executed: boolean
  data: unknown
  error: string | null
}

export interface RunbookDetail extends Runbook {
  content: string
}

export interface AuditEvent {
  id: string
  cluster_id: string
  action_type: string
  resource_target: string | null
  outcome: string | null
  actor_type: string | null
  actor_id: string | null
  details: string | null
  timestamp: string
}

export interface ServiceHealth {
  name: string
  workload: string
  status: "ok" | "warn" | "crit"
  rps: number | null
  error_pct: number | null
  p95_ms: number | null
  p99_ms: number | null
  cpu_pct: number | null
  mem_pct: number | null
}

export interface Runbook {
  id: string
  title: string
  service: string
  incident_type: string
  severity: string
  path: string
}

// ---------------------------------------------------------------------------

export const SEV = {
  critical: { label: "SEV-1", cls: "s1", tone: "crit" },
  high: { label: "SEV-2", cls: "s2", tone: "warn" },
  medium: { label: "SEV-3", cls: "s3", tone: "warn" },
  low: { label: "SEV-4", cls: "s4", tone: "sel" },
} as const

export function sev(s: Severity) {
  return SEV[s] ?? SEV.medium
}

export function statusBadge(s: IncidentStatusT): { label: string; cls: string } {
  if (s === "resolved") return { label: "Resolved", cls: "ok" }
  if (s === "investigating") return { label: "Investigating", cls: "sel" }
  return { label: "Open", cls: "crit" }
}

export function clusterStatusTone(s: Cluster["status"]): string {
  if (s === "online") return "var(--ok)"
  if (s === "maintenance") return "var(--warn)"
  return "var(--crit)"
}

export function cap(s: string): string {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s
}

/** "6m ago", "2h 14m ago", "3d ago" */
export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "—"
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return "—"
  const s = Math.max(0, Math.floor((Date.now() - then) / 1000))
  if (s < 60) return `${s}s ago`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ${m % 60}m ago`
  const d = Math.floor(h / 24)
  return `${d}d ${h % 24}h ago`
}

/** Elapsed clock "MM:SS" or "HH:MM" between start and (end|now). */
export function elapsed(startIso: string, endIso?: string | null): string {
  const start = new Date(startIso).getTime()
  const end = endIso ? new Date(endIso).getTime() : Date.now()
  let s = Math.max(0, Math.floor((end - start) / 1000))
  const h = Math.floor(s / 3600)
  s -= h * 3600
  const m = Math.floor(s / 60)
  s -= m * 60
  const p = (n: number) => String(n).padStart(2, "0")
  return h > 0 ? `${h}:${p(m)}:${p(s)}` : `${p(m)}:${p(s)}`
}

export function fmtTimeUTC(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return "—"
  return d.toISOString().slice(11, 19)
}

export function round(n: number | null | undefined, dp = 2): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—"
  return n.toFixed(dp)
}

/** Golden-signal error-rate → status tone. */
export function errTone(pct: number | null): "ok" | "warn" | "crit" {
  if (pct === null) return "ok"
  if (pct >= 5) return "crit"
  if (pct >= 1) return "warn"
  return "ok"
}

/** Error-rate → table cell colour class (matches .sx-tbl td.bad / td.warnc). */
export function errCls(pct: number | null): string {
  const t = errTone(pct)
  return t === "crit" ? "bad" : t === "warn" ? "warnc" : ""
}

/** Build a simple SVG polyline points string from a numeric series. */
export function sparkPoints(series: number[], w = 96, h = 22): string {
  if (!series.length) return `0,${h / 2} ${w},${h / 2}`
  const min = Math.min(...series)
  const max = Math.max(...series)
  const span = max - min || 1
  const step = w / Math.max(1, series.length - 1)
  return series
    .map((v, i) => `${(i * step).toFixed(1)},${(h - ((v - min) / span) * (h - 4) - 2).toFixed(1)}`)
    .join(" ")
}
