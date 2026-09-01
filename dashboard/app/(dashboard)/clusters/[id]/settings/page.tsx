"use client"

import { useCallback, useEffect, useState } from "react"
import { useParams } from "next/navigation"
import { api, useAuth } from "@/lib/auth-context"
import { useCluster } from "@/components/console/ClusterContext"
import { ConsolePage } from "@/components/console/ConsolePage"
import { SectionTitle } from "@/components/console/ui"

interface ConnCheck {
  name: string
  configured: boolean
  ok: boolean | null
  detail: string
}

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

  const [endpoints, setEndpoints] = useState({
    name: "",
    prometheus_url: "",
    loki_url: "",
    github_repo: "",
    notion_database_id: "",
    notion_api_key: "",
    namespace: "",
    llm_provider: "",
    llm_model: "",
    llm_base_url: "",
    llm_api_key: "",
  })
  const [ghInstalling, setGhInstalling] = useState(false)
  const [ghErr, setGhErr] = useState<string | null>(null)
  const [slackTeamId, setSlackTeamId] = useState<string | null>(null)
  const [slackInstalling, setSlackInstalling] = useState(false)
  const [slackErr, setSlackErr] = useState<string | null>(null)
  const [metrics, setMetrics] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [conns, setConns] = useState<ConnCheck[] | null>(null)
  const [checking, setChecking] = useState(false)

  const checkConnections = useCallback(async () => {
    setChecking(true)
    try {
      const { data } = await api.get<{ checks: ConnCheck[] }>(`/clusters/${id}/connections`)
      setConns(data.checks)
    } catch {
      setConns(null)
    } finally {
      setChecking(false)
    }
  }, [id])

  useEffect(() => {
    checkConnections()
  }, [checkConnections])

  useEffect(() => {
    api
      .get<{ slack_team_id: string | null }>("/organization")
      .then(({ data }) => setSlackTeamId(data.slack_team_id ?? null))
      .catch(() => setSlackTeamId(null))
  }, [])

  useEffect(() => {
    if (!cluster) return
    setEndpoints({
      name: cluster.name ?? "",
      prometheus_url: cluster.prometheus_url ?? "",
      loki_url: cluster.loki_url ?? "",
      github_repo: cluster.github_repo ?? "",
      notion_database_id: cluster.notion_database_id ?? "",
      notion_api_key: "",
      namespace: cluster.namespace ?? "",
      llm_provider: cluster.llm_provider ?? "",
      llm_model: cluster.llm_model ?? "",
      llm_base_url: cluster.llm_base_url ?? "",
      llm_api_key: "",
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
        notion_database_id: endpoints.notion_database_id || undefined,
        notion_api_key: endpoints.notion_api_key || undefined,
        metrics_config: cleanMetrics,
        // Sent even when blank so they can be cleared (revert to whole-cluster /
        // platform default). The API key is write-only: only sent when entered.
        namespace: endpoints.namespace,
        llm_provider: endpoints.llm_provider,
        llm_model: endpoints.llm_model,
        llm_base_url: endpoints.llm_base_url,
        llm_api_key: endpoints.llm_api_key || undefined,
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
        <SectionTitle
          title="Connections"
          meta="is this cluster actually wired up?"
          action={
            <button
              className="sx-btn"
              style={{ flex: "none", padding: "5px 10px", fontSize: 11.5 }}
              onClick={checkConnections}
              disabled={checking}
            >
              {checking ? "Checking…" : "Re-check"}
            </button>
          }
        />
        <div style={{ display: "flex", flexDirection: "column", gap: 2, marginTop: 10, marginBottom: 26 }}>
          {conns === null ? (
            <div className="sx-dry" style={{ textAlign: "left" }}>
              {checking ? "Checking connections…" : "Could not run the connection check."}
            </div>
          ) : (
            conns.map((c) => {
              const color = c.ok === true ? "var(--ok)" : c.ok === false ? "var(--crit)" : "var(--ink3)"
              const label = c.ok === true ? "OK" : c.ok === false ? "FAILING" : "not set"
              return (
                <div
                  key={c.name}
                  style={{ display: "flex", alignItems: "baseline", gap: 10, padding: "8px 0", borderBottom: "1px solid var(--rule)" }}
                >
                  <span style={{ width: 8, height: 8, borderRadius: "50%", background: color, flex: "none", transform: "translateY(2px)" }} />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 13, fontWeight: 500 }}>
                      {c.name}
                      <span className="sx-mono" style={{ fontSize: 10.5, color, marginLeft: 8, letterSpacing: ".06em" }}>{label}</span>
                    </div>
                    <div style={{ fontSize: 12, color: "var(--ink2)", marginTop: 1 }}>{c.detail}</div>
                  </div>
                </div>
              )
            })
          )}
        </div>

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
          <div>
            <label className="sx-label">GitHub App</label>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <button
                className="sx-btn"
                style={{ flex: "none", padding: "6px 12px", fontSize: 12 }}
                disabled={ghInstalling}
                onClick={async () => {
                  setGhInstalling(true)
                  setGhErr(null)
                  try {
                    const { data } = await api.get<{ install_url: string }>(`/clusters/${id}/github-app/install-url`)
                    window.location.href = data.install_url
                  } catch {
                    setGhErr("Could not start the GitHub App install flow.")
                    setGhInstalling(false)
                  }
                }}
              >
                {ghInstalling ? "Redirecting…" : cluster?.github_app_installation_id ? "Reinstall GitHub App" : "Install GitHub App"}
              </button>
              <span className="sx-mono" style={{ fontSize: 11, color: "var(--ink3)" }}>
                {cluster?.github_app_installation_id ? `installed (${cluster.github_app_installation_id})` : "not installed"}
              </span>
            </div>
            {ghErr && <div className="sx-dry" style={{ textAlign: "left", marginTop: 6, color: "var(--crit)" }}>{ghErr}</div>}
            <div style={{ color: "var(--ink2)", fontSize: 11.5, marginTop: 6, lineHeight: 1.5 }}>
              Installing the GitHub App mints short-lived tokens per request instead of a static PAT — preferred over a raw GitHub token.
            </div>
          </div>
        </div>

        <SectionTitle title="Scope" meta="what this cluster monitors" />
        <p style={{ color: "var(--ink2)", fontSize: 12.5, marginTop: 8, lineHeight: 1.6 }}>
          Required. Every cluster is scoped to one Kubernetes namespace — its metrics, service view, and remediation blast radius are limited to that namespace. That&apos;s how multiple apps on the same Kubernetes each become their own cluster, and how tenants stay isolated from each other.
        </p>
        <div style={{ display: "flex", flexDirection: "column", gap: 14, marginTop: 12, maxWidth: 320 }}>
          <div>
            <label className="sx-label">Namespace</label>
            <input className="sx-input sx-mono" style={{ fontSize: 12 }} placeholder="e.g. production" value={endpoints.namespace} onChange={(e) => setEndpoints({ ...endpoints, namespace: e.target.value })} />
          </div>
        </div>

        <SectionTitle title="Agent brain" meta="per-cluster LLM override (optional)" />
        <p style={{ color: "var(--ink2)", fontSize: 12.5, marginTop: 8, lineHeight: 1.6 }}>
          Leave the provider blank to use the platform default. Overrides are validated against operator policy at run start and recorded as configured runtime metadata; deterministic router pinning is handled separately.
        </p>
        <div style={{ display: "flex", flexDirection: "column", gap: 14, marginTop: 12, maxWidth: 420 }}>
          <div>
            <label className="sx-label">Provider</label>
            <select className="sx-input" value={endpoints.llm_provider} onChange={(e) => setEndpoints({ ...endpoints, llm_provider: e.target.value })}>
              <option value="">Platform default</option>
              <option value="anthropic">anthropic (Claude)</option>
              <option value="gemini">gemini</option>
            </select>
          </div>
          <div>
            <label className="sx-label">Model</label>
            <input className="sx-input sx-mono" style={{ fontSize: 12 }} placeholder="(provider default)" value={endpoints.llm_model} onChange={(e) => setEndpoints({ ...endpoints, llm_model: e.target.value })} />
          </div>
          <div>
            <label className="sx-label">Base URL (self-hosted)</label>
            <input className="sx-input sx-mono" style={{ fontSize: 12 }} placeholder="http://host:11434/v1" value={endpoints.llm_base_url} onChange={(e) => setEndpoints({ ...endpoints, llm_base_url: e.target.value })} />
          </div>
          <div>
            <label className="sx-label">API key</label>
            <input className="sx-input sx-mono" style={{ fontSize: 12 }} type="password" placeholder={cluster?.llm_provider ? "•••••• (set — leave blank to keep)" : "optional"} value={endpoints.llm_api_key} onChange={(e) => setEndpoints({ ...endpoints, llm_api_key: e.target.value })} />
          </div>
        </div>

        <SectionTitle title="Runbooks" meta="Notion (required — no runbooks without it)" />
        <p style={{ color: "var(--ink2)", fontSize: 12.5, marginTop: 8, lineHeight: 1.6 }}>
          Point Sentinel at your team&apos;s runbook database in Notion. Share the database with a Notion integration and paste its token + the database ID. Notion is the only runbook source — without this configured, the catalog stays empty and the agent has no runbooks to search.
        </p>
        <div style={{ display: "flex", flexDirection: "column", gap: 14, marginTop: 12 }}>
          <div>
            <label className="sx-label">Notion database ID</label>
            <input className="sx-input sx-mono" style={{ fontSize: 12 }} placeholder="32-char database id" value={endpoints.notion_database_id} onChange={(e) => setEndpoints({ ...endpoints, notion_database_id: e.target.value })} />
          </div>
          <div>
            <label className="sx-label">Notion integration token</label>
            <input className="sx-input sx-mono" style={{ fontSize: 12 }} type="password" placeholder={cluster?.notion_database_id ? "•••••• (set — leave blank to keep)" : "secret_..."} value={endpoints.notion_api_key} onChange={(e) => setEndpoints({ ...endpoints, notion_api_key: e.target.value })} />
          </div>
        </div>

        <SectionTitle title="Slack" meta="incident notifications for the organization" />
        <p style={{ color: "var(--ink2)", fontSize: 12.5, marginTop: 8, lineHeight: 1.6 }}>
          Connect Slack once per organization — every cluster&apos;s incidents post to it. Opens a Slack window to pick the workspace and channel.
        </p>
        <div style={{ marginTop: 12 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <button
              className="sx-btn"
              style={{ flex: "none", padding: "6px 12px", fontSize: 12 }}
              disabled={slackInstalling}
              onClick={async () => {
                setSlackInstalling(true)
                setSlackErr(null)
                try {
                  const { data } = await api.get<{ install_url: string }>("/organizations/slack/install-url")
                  window.location.href = data.install_url
                } catch {
                  setSlackErr("Could not start the Slack install flow.")
                  setSlackInstalling(false)
                }
              }}
            >
              {slackInstalling ? "Redirecting…" : slackTeamId ? "Reconnect Slack" : "Connect Slack"}
            </button>
            <span className="sx-mono" style={{ fontSize: 11, color: "var(--ink3)" }}>
              {slackTeamId ? `connected (${slackTeamId})` : "not connected"}
            </span>
          </div>
          {slackErr && <div className="sx-dry" style={{ textAlign: "left", marginTop: 6, color: "var(--crit)" }}>{slackErr}</div>}
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
