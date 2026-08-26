"use client"

import { useCallback, useEffect, useState, type CSSProperties } from "react"
import { useRouter } from "next/navigation"
import { api, useAuth } from "@/lib/auth-context"
import { Spinner } from "@/components/console/ui"
import { clusterStatusTone, type Cluster } from "@/lib/console"

export default function HomeGate() {
  const router = useRouter()
  const { user, logout } = useAuth()
  const [clusters, setClusters] = useState<Cluster[] | null>(null)
  const [creating, setCreating] = useState(false)
  const [token, setToken] = useState<string | null>(null)
  const [form, setForm] = useState({ name: "", prometheus_url: "", loki_url: "", github_repo: "", github_token: "" })
  const [createdId, setCreatedId] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const { data } = await api.get<Cluster[]>("/clusters")
      setClusters(data)
    } catch {
      setClusters([])
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const create = async () => {
    setErr(null)
    setCreating(true)
    try {
      const payload: Record<string, string> = { name: form.name }
      if (form.prometheus_url) payload.prometheus_url = form.prometheus_url
      if (form.loki_url) payload.loki_url = form.loki_url
      if (form.github_repo) payload.github_repo = form.github_repo
      if (form.github_token) payload.github_token = form.github_token
      const { data } = await api.post<{ id: string; name: string; token: string }>("/clusters", payload)
      setToken(data.token)
      setCreatedId(data.id)
    } catch (e) {
      const ax = e as { response?: { data?: { detail?: string } } }
      setErr(ax.response?.data?.detail ?? "Could not create cluster.")
    } finally {
      setCreating(false)
    }
  }

  // More than one cluster: pick list. Exactly one: jump straight into it.
  useEffect(() => {
    if (clusters && clusters.length === 1) router.replace(`/clusters/${clusters[0].id}`)
  }, [clusters, router])

  if (clusters === null) {
    return (
      <div style={{ minHeight: "100vh", background: "var(--paper)", display: "flex", alignItems: "center", justifyContent: "center" }} className="sx-app-font">
        <Spinner />
      </div>
    )
  }

  const shellStyle: CSSProperties = {
    minHeight: "100vh",
    background: "var(--paper)",
    color: "var(--ink)",
    fontFamily: "var(--font-sans), 'Hanken Grotesk', sans-serif",
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    padding: "72px 24px",
  }

  // Cluster picker (2+ clusters)
  if (clusters.length > 1) {
    return (
      <div style={shellStyle}>
        <div style={{ width: "100%", maxWidth: 560 }}>
          <div className="sx-wordmark" style={{ fontSize: 22, marginBottom: 4 }}>
            <span className="tick" /> Sentinel
          </div>
          <div className="sx-sc" style={{ marginBottom: 22 }}>Select a cluster</div>
          {clusters.map((c) => (
            <button
              key={c.id}
              onClick={() => router.push(`/clusters/${c.id}`)}
              style={{ display: "flex", alignItems: "center", gap: 12, width: "100%", textAlign: "left", background: "var(--panel)", border: "1px solid var(--rule2)", borderRadius: 2, padding: "14px 16px", marginBottom: 10, cursor: "pointer", color: "var(--ink)" }}
            >
              <span className="sx-sq" style={{ background: clusterStatusTone(c.status) }} />
              <span style={{ flex: 1, fontWeight: 500 }}>{c.name}</span>
              <span className="sx-mono" style={{ fontSize: 11, color: "var(--ink3)" }}>{c.status}</span>
            </button>
          ))}
          <button onClick={logout} className="sx-back" style={{ marginTop: 16 }}>Sign out ({user?.email})</button>
        </div>
      </div>
    )
  }

  // No clusters → connect onboarding
  return (
    <div style={shellStyle}>
      <div style={{ width: "100%", maxWidth: 520 }}>
        <div className="sx-wordmark" style={{ fontSize: 22, marginBottom: 4 }}>
          <span className="tick" /> Sentinel
        </div>
        <h1 style={{ fontSize: 24, fontWeight: 600, margin: "18px 0 6px" }}>Connect a cluster</h1>
        <p style={{ color: "var(--ink2)", fontSize: 13.5, marginTop: 0, marginBottom: 24, lineHeight: 1.6 }}>
          Register your infrastructure endpoints. Sentinel opens incidents from your telemetry — point your Alertmanager webhook at the platform with the token you receive.
        </p>

        {!token ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            <div>
              <label className="sx-label">Cluster name</label>
              <input className="sx-input" placeholder="prod-us-east" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
            </div>
            <div>
              <label className="sx-label">Prometheus URL</label>
              <input className="sx-input" placeholder="https://prometheus.your-infra:9090" value={form.prometheus_url} onChange={(e) => setForm({ ...form, prometheus_url: e.target.value })} />
            </div>
            <div>
              <label className="sx-label">Loki URL</label>
              <input className="sx-input" placeholder="https://loki.your-infra:3100" value={form.loki_url} onChange={(e) => setForm({ ...form, loki_url: e.target.value })} />
            </div>
            <div>
              <label className="sx-label">GitHub repo (optional)</label>
              <input className="sx-input" placeholder="org/repo" value={form.github_repo} onChange={(e) => setForm({ ...form, github_repo: e.target.value })} />
            </div>
            {err && <div className="sx-empty" style={{ borderColor: "var(--crit-t)", color: "var(--crit)", padding: 14 }}>{err}</div>}
            <button className="sx-btn primary" style={{ maxWidth: 180 }} onClick={create} disabled={creating || !form.name.trim()}>
              {creating ? "Connecting…" : "Connect cluster"}
            </button>
          </div>
        ) : (
          <div>
            <div className="sx-card" style={{ borderColor: "var(--ok)", background: "var(--ok-t)" }}>
              <div style={{ fontWeight: 600, marginBottom: 6 }}>Cluster connected</div>
              <p style={{ fontSize: 12.5, color: "#2f5238", margin: "0 0 10px", lineHeight: 1.6 }}>
                Save this cluster token — it authenticates your Alertmanager webhook and won’t be shown again.
              </p>
              <div className="sx-mono" style={{ background: "var(--paper)", border: "1px solid var(--rule2)", padding: "10px 12px", borderRadius: 2, fontSize: 12, wordBreak: "break-all" }}>
                {token}
              </div>
            </div>
            <button className="sx-btn primary" style={{ maxWidth: 180, marginTop: 16 }} onClick={() => createdId && router.push(`/clusters/${createdId}`)}>
              Open console →
            </button>
          </div>
        )}

        <button onClick={logout} className="sx-back" style={{ marginTop: 28 }}>Sign out ({user?.email})</button>
      </div>
    </div>
  )
}
