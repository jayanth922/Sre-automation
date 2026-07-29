"use client"

import { useEffect, useState } from "react"
import { useParams } from "next/navigation"
import { api, useAuth } from "@/lib/auth-context"
import { useCluster } from "@/components/console/ClusterContext"
import { ConsolePage } from "@/components/console/ConsolePage"
import { SectionTitle } from "@/components/console/ui"

// Mirrors backend defaults (sre_agent/metrics_profile.DEFAULTS). Shown as
// placeholders so a blank field means "use the platform default".
const METRIC_FIELDS: { key: string; label: string; def: string }[] = [
  { key: "service_label", label: "Service label", def: "service" },
  { key: "request_metric", label: "Request counter metric", def: "http_requests_total" },
  { key: "status_label", label: "Status label", def: "status" },
  { key: "error_regex", label: "Error status selector", def: "5.." },
  { key: "latency_histogram", label: "Latency histogram metric", def: "http_request_duration_seconds" },
  { key: "cpu_query", label: "CPU saturation query", def: "avg(rate(container_cpu_usage_seconds_total[5m])) * 100" },
  { key: "mem_query", label: "Memory query", def: "sum(container_memory_usage_bytes) / (1024*1024*1024)" },
]

export default function SettingsPage() {
  const { id } = useParams<{ id: string }>()
  const cluster = useCluster()
  const { user } = useAuth()
  const isAdmin = (user?.role ?? "member") === "admin"

  const [endpoints, setEndpoints] = useState({ name: "", prometheus_url: "", loki_url: "", github_repo: "" })
  const [metrics, setMetrics] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    if (!cluster) return
    setEndpoints({
      name: cluster.name ?? "",
      prometheus_url: cluster.prometheus_url ?? "",
      loki_url: cluster.loki_url ?? "",
      github_repo: cluster.github_repo ?? "",
    })
    try {
      setMetrics(cluster.metrics_config ? (JSON.parse(cluster.metrics_config) as Record<string, string>) : {})
    } catch {
      setMetrics({})
    }
  }, [cluster])

  const save = async () => {
    setSaving(true)
    setSaved(false)
    setErr(null)
    try {
      const cleanMetrics: Record<string, string> = {}
      for (const f of METRIC_FIELDS) {
        const v = (metrics[f.key] ?? "").trim()
        if (v) cleanMetrics[f.key] = v
      }
      await api.patch(`/clusters/${id}`, {
        name: endpoints.name || undefined,
        prometheus_url: endpoints.prometheus_url || undefined,
        loki_url: endpoints.loki_url || undefined,
        github_repo: endpoints.github_repo || undefined,
        metrics_config: cleanMetrics,
      })
      setSaved(true)
      setTimeout(() => window.location.reload(), 700)
    } catch (e) {
      const ax = e as { response?: { data?: { detail?: string }; status?: number } }
      setErr(ax.response?.status === 403 ? "Only admins can change cluster settings." : ax.response?.data?.detail ?? "Could not save.")
    } finally {
      setSaving(false)
    }
  }

  return (
    <ConsolePage title="Settings">
      <div style={{ maxWidth: 620 }}>
        <SectionTitle title="Endpoints" meta="how the platform reaches your infrastructure" />
        <div style={{ display: "flex", flexDirection: "column", gap: 14, marginTop: 12 }}>
          <div>
            <label className="sx-label">Cluster name</label>
            <input className="sx-input" value={endpoints.name} onChange={(e) => setEndpoints({ ...endpoints, name: e.target.value })} />
          </div>
          <div>
            <label className="sx-label">Prometheus URL</label>
            <input className="sx-input" placeholder="https://prometheus.your-infra:9090" value={endpoints.prometheus_url} onChange={(e) => setEndpoints({ ...endpoints, prometheus_url: e.target.value })} />
          </div>
          <div>
            <label className="sx-label">Loki URL</label>
            <input className="sx-input" placeholder="https://loki.your-infra:3100" value={endpoints.loki_url} onChange={(e) => setEndpoints({ ...endpoints, loki_url: e.target.value })} />
          </div>
          <div>
            <label className="sx-label">GitHub repo</label>
            <input className="sx-input" placeholder="org/repo" value={endpoints.github_repo} onChange={(e) => setEndpoints({ ...endpoints, github_repo: e.target.value })} />
          </div>
        </div>

        <SectionTitle title="Observability profile" meta="your Prometheus conventions" />
        <p style={{ color: "var(--ink2)", fontSize: 12.5, marginTop: 8, lineHeight: 1.6 }}>
          Leave a field blank to use the platform default (shown as the placeholder). These map the golden-signal and per-service queries onto whatever metric names your workloads emit — the platform doesn’t assume any particular schema.
        </p>
        <div style={{ display: "flex", flexDirection: "column", gap: 14, marginTop: 12 }}>
          {METRIC_FIELDS.map((f) => (
            <div key={f.key}>
              <label className="sx-label">{f.label}</label>
              <input
                className="sx-input sx-mono"
                style={{ fontSize: 12 }}
                placeholder={f.def}
                value={metrics[f.key] ?? ""}
                onChange={(e) => setMetrics({ ...metrics, [f.key]: e.target.value })}
              />
            </div>
          ))}
        </div>

        {err && <div className="sx-empty" style={{ borderColor: "var(--crit-t)", color: "var(--crit)", padding: 14, marginTop: 18 }}>{err}</div>}

        <div className="sx-btnrow" style={{ marginTop: 20, maxWidth: 260 }}>
          <button className="sx-btn primary" onClick={save} disabled={!isAdmin || saving}>
            {saving ? "Saving…" : saved ? "Saved ✓" : "Save changes"}
          </button>
        </div>
        {!isAdmin && <div className="sx-dry" style={{ textAlign: "left", marginTop: 8 }}>Only admins can change cluster settings.</div>}
      </div>
    </ConsolePage>
  )
}
