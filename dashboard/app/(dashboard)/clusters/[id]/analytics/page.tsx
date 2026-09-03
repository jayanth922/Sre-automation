"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import { useParams } from "next/navigation"
import { api } from "@/lib/auth-context"
import { useLiveStream } from "@/lib/useLiveStream"
import { ConsolePage } from "@/components/console/ConsolePage"
import { SectionTitle, Spinner, Empty, ErrorNote, useFreshness } from "@/components/console/ui"
import type { Analytics, Recommendations } from "@/lib/console"

export default function AnalyticsPage() {
  const { id } = useParams<{ id: string }>()
  const { events, connected } = useLiveStream(undefined, { channel: "incidents" })
  const [a, setA] = useState<Analytics | null>(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState(false)
  const [recs, setRecs] = useState<Recommendations | null>(null)
  const [recsLoading, setRecsLoading] = useState(true)
  const [updatedAt, setUpdatedAt] = useState<number>(Date.now())
  const lastLen = useRef(0)

  // Aggregate stats only — cheap, safe to refetch on every incident event.
  const reloadAnalytics = useCallback(async () => {
    try {
      const { data } = await api.get<Analytics>(`/clusters/${id}/analytics`)
      setA(data)
      setErr(false)
      setUpdatedAt(Date.now())
    } catch {
      setErr(true)
    } finally {
      setLoading(false)
    }
  }, [id])

  const load = useCallback(async () => {
    await reloadAnalytics()
    // Recommendations are an LLM call — load once, not on every live event.
    try {
      const { data } = await api.get<Recommendations>(`/clusters/${id}/recommendations`)
      setRecs(data)
    } catch {
      /* recommendations are best-effort (LLM) */
    } finally {
      setRecsLoading(false)
    }
  }, [id, reloadAnalytics])

  useEffect(() => {
    load()
  }, [load])

  // Refresh the stats the moment an incident opens or resolves.
  useEffect(() => {
    if (events.length && events.length !== lastLen.current) {
      lastLen.current = events.length
      reloadAnalytics()
    }
  }, [events.length, reloadAnalytics])

  const freshness = useFreshness(updatedAt)

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
    <ConsolePage title="Analytics" live={connected} updated={freshness}>
      {loading ? (
        <Spinner />
      ) : err ? (
        <ErrorNote>Couldn’t load analytics — the API may be unreachable.</ErrorNote>
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
