"use client"

import { useCallback, useEffect, useState } from "react"
import { useParams } from "next/navigation"
import { api } from "@/lib/auth-context"
import { ConsolePage } from "@/components/console/ConsolePage"
import { SectionTitle, Spinner, Empty } from "@/components/console/ui"
import type { Analytics, Recommendations } from "@/lib/console"

export default function AnalyticsPage() {
  const { id } = useParams<{ id: string }>()
  const [a, setA] = useState<Analytics | null>(null)
  const [loading, setLoading] = useState(true)
  const [recs, setRecs] = useState<Recommendations | null>(null)
  const [recsLoading, setRecsLoading] = useState(true)

  const load = useCallback(async () => {
    try {
      const { data } = await api.get<Analytics>(`/clusters/${id}/analytics`)
      setA(data)
    } finally {
      setLoading(false)
    }
    try {
      const { data } = await api.get<Recommendations>(`/clusters/${id}/recommendations`)
      setRecs(data)
    } catch {
      /* recommendations are best-effort (LLM) */
    } finally {
      setRecsLoading(false)
    }
  }, [id])

  useEffect(() => {
    load()
  }, [load])

  const maxWeek = a ? Math.max(1, ...a.weekly_incidents.map((w) => w.count)) : 1
  const maxSev = a ? Math.max(1, ...a.severity_distribution.map((s) => s.count)) : 1

  const stat = (label: string, value: string, unit?: string) => (
    <div className="sx-sig">
      <div className="k">{label}</div>
      <div className="v">
        {value}
        {unit && <span className="u">{unit}</span>}
      </div>
    </div>
  )

  return (
    <ConsolePage title="Analytics">
      {loading ? (
        <Spinner />
      ) : !a || a.stats.total_incidents === 0 ? (
        <Empty>No incident history yet — analytics appear once incidents accrue.</Empty>
      ) : (
        <>
          <div className="sx-signals" style={{ gridTemplateColumns: "repeat(4,1fr)" }}>
            {stat("Total incidents", String(a.stats.total_incidents))}
            {stat("Resolved", String(a.stats.resolved))}
            {stat("Resolution rate", String(a.stats.resolution_rate_pct), "%")}
            {stat("MTTR", String(a.stats.mttr_minutes), "min")}
          </div>

          <div className="sx-two">
            <div>
              <SectionTitle title="Incidents by week" />
              <div className="sx-bud">
                {a.weekly_incidents.length === 0 ? (
                  <Empty>No weekly data.</Empty>
                ) : (
                  a.weekly_incidents.map((w) => (
                    <div className="row" key={w.week}>
                      <div className="rt">
                        <span>{w.week}</span>
                        <span className="p">{w.count}</span>
                      </div>
                      <div className="sx-track">
                        <span style={{ width: `${(w.count / maxWeek) * 100}%`, background: "var(--sel)" }} />
                      </div>
                    </div>
                  ))
                )}
              </div>

              <SectionTitle title="Severity distribution" />
              <div className="sx-bud">
                {a.severity_distribution.map((s) => (
                  <div className="row" key={s.severity}>
                    <div className="rt">
                      <span>{s.severity}</span>
                      <span className="p">{s.count}</span>
                    </div>
                    <div className="sx-track">
                      <span style={{ width: `${(s.count / maxSev) * 100}%`, background: "var(--warn)" }} />
                    </div>
                  </div>
                ))}
              </div>
            </div>

            <div>
              <SectionTitle title="Top recurring alerts" />
              {a.top_alerts.length === 0 ? (
                <Empty>None.</Empty>
              ) : (
                <table className="sx-tbl">
                  <thead>
                    <tr>
                      <th className="l">Alert</th>
                      <th>Count</th>
                    </tr>
                  </thead>
                  <tbody>
                    {a.top_alerts.map((t) => (
                      <tr key={t.title}>
                        <td className="l">{t.title}</td>
                        <td>{t.count}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}

              <SectionTitle title="Recommendations" meta="AI · reliability advice" />
              {recsLoading ? (
                <Spinner />
              ) : !recs ? (
                <Empty>Recommendations unavailable.</Empty>
              ) : (
                <div className="sx-card" style={{ whiteSpace: "pre-wrap", fontSize: 12.5, lineHeight: 1.7, color: "var(--ink)" }}>
                  {recs.recommendations}
                </div>
              )}
            </div>
          </div>
        </>
      )}
    </ConsolePage>
  )
}
