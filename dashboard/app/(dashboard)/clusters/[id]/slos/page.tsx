"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import { useParams } from "next/navigation"
import { api } from "@/lib/auth-context"
import { useLiveStream } from "@/lib/useLiveStream"
import { ConsolePage } from "@/components/console/ConsolePage"
import { SectionTitle, Spinner, Empty, ErrorNote, useFreshness } from "@/components/console/ui"
import { type SLO, type SLOStatus, round } from "@/lib/console"

const emptyForm = { name: "", sli_metric: "", target: "99.9", window_days: "30" }

interface Row {
  slo: SLO
  remaining: number
  breaching: boolean
  tone: "ok" | "warn" | "crit"
}

export default function SlosPage() {
  const { id } = useParams<{ id: string }>()
  const { events, connected } = useLiveStream(undefined, { channel: "incidents" })
  const [rows, setRows] = useState<Row[]>([])
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState(false)
  const [updatedAt, setUpdatedAt] = useState<number>(Date.now())
  const lastLen = useRef(0)
  const [showForm, setShowForm] = useState(false)
  const [form, setForm] = useState(emptyForm)
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const { data: slos } = await api.get<SLO[]>(`/clusters/${id}/slos`)
      const built = await Promise.all(
        slos.map(async (s) => {
          try {
            const { data } = await api.get<SLOStatus>(`/clusters/${id}/slos/${s.id}/status`)
            const remaining = Math.max(0, Math.round(100 - data.budget_consumed_percent))
            const tone: Row["tone"] = remaining < 15 ? "crit" : remaining < 40 ? "warn" : "ok"
            return { slo: s, remaining, breaching: data.is_breaching, tone }
          } catch {
            return { slo: s, remaining: 100, breaching: false, tone: "ok" as const }
          }
        }),
      )
      setRows(built)
      setErr(false)
      setUpdatedAt(Date.now())
    } catch {
      setErr(true)
    } finally {
      setLoading(false)
    }
  }, [id])

  useEffect(() => {
    load()
    const t = setInterval(load, 20000)
    return () => clearInterval(t)
  }, [load])

  // A firing/clearing incident often coincides with an error-budget change —
  // refresh burn rates immediately rather than on the next 20s tick.
  useEffect(() => {
    if (events.length && events.length !== lastLen.current) {
      lastLen.current = events.length
      load()
    }
  }, [events.length, load])

  const freshness = useFreshness(updatedAt)

  const createSlo = async () => {
    setCreateError(null)
    const target = parseFloat(form.target)
    const windowDays = parseInt(form.window_days, 10)
    if (!form.name.trim() || !form.sli_metric.trim()) {
      setCreateError("Name and SLI metric are required.")
      return
    }
    if (!Number.isFinite(target) || target <= 0 || target > 100) {
      setCreateError("Target must be a percentage between 0 and 100.")
      return
    }
    if (!Number.isFinite(windowDays) || windowDays <= 0) {
      setCreateError("Window (days) must be a positive number.")
      return
    }
    setCreating(true)
    try {
      await api.post(`/clusters/${id}/slos`, {
        name: form.name.trim(),
        sli_metric: form.sli_metric.trim(),
        target,
        window_days: windowDays,
      })
      setForm(emptyForm)
      setShowForm(false)
      await load()
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      setCreateError(detail || "Could not create SLO.")
    } finally {
      setCreating(false)
    }
  }

  const newSloButton = (
    <button
      type="button"
      className="sx-btn"
      style={{ flex: "none", padding: "5px 10px", fontSize: 11.5 }}
      onClick={() => {
        setCreateError(null)
        setShowForm((v) => !v)
      }}
    >
      {showForm ? "Cancel" : "New SLO"}
    </button>
  )

  const form_panel = showForm && (
    <div className="sx-dry" style={{ textAlign: "left", marginBottom: 18 }}>
      <div style={{ display: "flex", flexDirection: "column", gap: 12, maxWidth: 420 }}>
        <div>
          <label className="sx-label" htmlFor="slo-name">Objective name</label>
          <input
            id="slo-name"
            className="sx-input"
            placeholder="e.g. Checkout availability"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
          />
        </div>
        <div>
          <label className="sx-label" htmlFor="slo-metric">SLI query (raw PromQL, resolves to a 0-100 value)</label>
          <input
            id="slo-metric"
            className="sx-input sx-mono"
            style={{ fontSize: 12 }}
            placeholder='e.g. 100 * (1 - sum(rate(http_requests_total{status=~"5.."}[5m])) / sum(rate(http_requests_total[5m])))'
            value={form.sli_metric}
            onChange={(e) => setForm({ ...form, sli_metric: e.target.value })}
          />
          <small style={{ color: "var(--ink3)", fontSize: 10.5 }}>
            Evaluated against this cluster's Prometheus every 20s. Needs a Prometheus URL saved on the Infrastructure tab — without one, Current stays at the last value ever recorded.
          </small>
        </div>
        <div style={{ display: "flex", gap: 12 }}>
          <div style={{ flex: 1 }}>
            <label className="sx-label" htmlFor="slo-target">Target %</label>
            <input
              id="slo-target"
              className="sx-input sx-mono"
              style={{ fontSize: 12 }}
              placeholder="99.9"
              value={form.target}
              onChange={(e) => setForm({ ...form, target: e.target.value })}
            />
          </div>
          <div style={{ flex: 1 }}>
            <label className="sx-label" htmlFor="slo-window">Window (days)</label>
            <input
              id="slo-window"
              className="sx-input sx-mono"
              style={{ fontSize: 12 }}
              placeholder="30"
              value={form.window_days}
              onChange={(e) => setForm({ ...form, window_days: e.target.value })}
            />
          </div>
        </div>
        {createError && <div style={{ color: "var(--crit)", fontSize: 11 }}>{createError}</div>}
        <button
          type="button"
          className="sx-btn primary"
          style={{ flex: "none", padding: "7px 18px", alignSelf: "flex-start" }}
          onClick={createSlo}
          disabled={creating}
        >
          {creating ? "Creating…" : "Create SLO"}
        </button>
      </div>
    </div>
  )

  return (
    <ConsolePage title="Service level objectives" live={connected} updated={freshness}>
      {loading ? (
        <Spinner />
      ) : err ? (
        <ErrorNote>Couldn’t load SLOs — the API may be unreachable.</ErrorNote>
      ) : rows.length === 0 ? (
        <>
          <SectionTitle title="Objectives" meta="0 tracked" action={newSloButton} />
          {form_panel}
          <Empty>No SLOs defined for this cluster yet.</Empty>
        </>
      ) : (
        <>
          <SectionTitle title="Objectives" meta={`${rows.length} tracked`} action={newSloButton} />
          {form_panel}
          <table className="sx-tbl">
            <thead>
              <tr>
                <th className="l">Objective</th>
                <th className="l">SLI</th>
                <th>Target</th>
                <th>Current</th>
                <th className="l" style={{ width: 200 }}>
                  Error budget
                </th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.slo.id}>
                  <td className="l" style={{ fontWeight: 500 }}>
                    {r.slo.name}
                  </td>
                  <td className="l" style={{ color: "var(--ink2)" }}>
                    {r.slo.sli_metric}
                  </td>
                  <td>{r.slo.target}%</td>
                  <td className={r.tone === "crit" ? "bad" : r.tone === "warn" ? "warnc" : ""}>{r.slo.current_value === null ? "—" : `${round(r.slo.current_value, 2)}%`}</td>
                  <td className="l">
                    <div className="sx-track" style={{ height: 6 }}>
                      <span style={{ width: `${r.remaining}%`, background: `var(--${r.tone})` }} />
                    </div>
                    <small className="sx-mono" style={{ fontSize: 10, color: "var(--ink3)" }}>
                      {r.remaining}% remaining · {r.slo.window_days}d
                    </small>
                  </td>
                  <td>
                    <span className={`sx-badge ${r.breaching ? "crit" : r.tone}`}>{r.breaching ? "Breaching" : r.tone === "ok" ? "Healthy" : "At risk"}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </ConsolePage>
  )
}
