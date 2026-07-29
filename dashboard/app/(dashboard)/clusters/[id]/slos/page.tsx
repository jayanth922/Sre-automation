"use client"

import { useCallback, useEffect, useState } from "react"
import { useParams } from "next/navigation"
import { api } from "@/lib/auth-context"
import { ConsolePage } from "@/components/console/ConsolePage"
import { SectionTitle, Spinner, Empty } from "@/components/console/ui"
import { type SLO, type SLOStatus, round } from "@/lib/console"

interface Row {
  slo: SLO
  remaining: number
  breaching: boolean
  tone: "ok" | "warn" | "crit"
}

export default function SlosPage() {
  const { id } = useParams<{ id: string }>()
  const [rows, setRows] = useState<Row[]>([])
  const [loading, setLoading] = useState(true)

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
    } finally {
      setLoading(false)
    }
  }, [id])

  useEffect(() => {
    load()
    const t = setInterval(load, 20000)
    return () => clearInterval(t)
  }, [load])

  return (
    <ConsolePage title="Service level objectives">
      {loading ? (
        <Spinner />
      ) : rows.length === 0 ? (
        <Empty>No SLOs defined for this cluster yet.</Empty>
      ) : (
        <>
          <SectionTitle title="Objectives" meta={`${rows.length} tracked`} />
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
