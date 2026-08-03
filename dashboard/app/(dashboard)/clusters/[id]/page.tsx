"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import Link from "next/link"
import { useParams } from "next/navigation"
import { api } from "@/lib/auth-context"
import { useLiveStream } from "@/lib/useLiveStream"
import { ConsolePage } from "@/components/console/ConsolePage"
import { SectionTitle, Spinner, Empty, Sparkline, useFreshness } from "@/components/console/ui"
import {
  type Incident,
  type SLO,
  type SLOStatus,
  type ServiceHealth,
  type AuditEvent,
  type Analytics,
  type MetricsSnapshot,
  sev,
  statusBadge,
  timeAgo,
  elapsed,
  round,
  errCls,
} from "@/lib/console"

interface BudgetRow {
  slo: SLO
  remaining: number
  tone: "ok" | "warn" | "crit"
}

export default function OverviewPage() {
  const { id } = useParams<{ id: string }>()
  const { events, connected } = useLiveStream(undefined, { channel: "incidents" })
  const [metrics, setMetrics] = useState<MetricsSnapshot | null>(null)
  const [metricsErr, setMetricsErr] = useState(false)
  const [incidents, setIncidents] = useState<Incident[]>([])
  const [services, setServices] = useState<ServiceHealth[]>([])
  const [budgets, setBudgets] = useState<BudgetRow[]>([])
  const [audit, setAudit] = useState<AuditEvent[]>([])
  const [analytics, setAnalytics] = useState<Analytics | null>(null)
  const [loading, setLoading] = useState(true)
  const [updatedAt, setUpdatedAt] = useState<number>(Date.now())
  // Rolling client-side history so the golden signals show a live trend, not just
  // a current value — captures a spike as it happens while you watch.
  const [hist, setHist] = useState<{ errors: number[]; latency: number[]; cpu: number[] }>({ errors: [], latency: [], cpu: [] })
  const lastLen = useRef(0)

  const load = useCallback(async () => {
    const [m, inc, svc, slos, aud, ana] = await Promise.allSettled([
      api.get<MetricsSnapshot>(`/clusters/${id}/metrics`),
      api.get<Incident[]>(`/clusters/${id}/incidents`),
      api.get<ServiceHealth[]>(`/clusters/${id}/services`),
      api.get<SLO[]>(`/clusters/${id}/slos`),
      api.get<AuditEvent[]>(`/clusters/${id}/audit`, { params: { limit: 8 } }),
      api.get<Analytics>(`/clusters/${id}/analytics`),
    ])

    if (m.status === "fulfilled") {
      const md = m.value.data
      setMetrics(md)
      setMetricsErr(false)
      setHist((h) => ({
        errors: [...h.errors, md.errors ?? 0].slice(-30),
        latency: [...h.latency, md.latency ?? 0].slice(-30),
        cpu: [...h.cpu, md.cpu ?? 0].slice(-30),
      }))
    } else setMetricsErr(true)
    if (inc.status === "fulfilled") setIncidents(inc.value.data)
    if (svc.status === "fulfilled") setServices(svc.value.data)
    if (aud.status === "fulfilled") setAudit(aud.value.data)
    if (ana.status === "fulfilled") setAnalytics(ana.value.data)

    if (slos.status === "fulfilled") {
      const rows = await Promise.all(
        slos.value.data.map(async (s) => {
          try {
            const { data } = await api.get<SLOStatus>(`/clusters/${id}/slos/${s.id}/status`)
            const remaining = Math.max(0, Math.round(100 - data.budget_consumed_percent))
            const tone: BudgetRow["tone"] = remaining < 15 ? "crit" : remaining < 40 ? "warn" : "ok"
            return { slo: s, remaining, tone }
          } catch {
            return { slo: s, remaining: 100, tone: "ok" as const }
          }
        }),
      )
      setBudgets(rows)
    }
    setUpdatedAt(Date.now())
    setLoading(false)
  }, [id])

  useEffect(() => {
    load()
    const t = setInterval(load, 15000)
    return () => clearInterval(t)
  }, [load])

  // Refetch the moment an incident opens or resolves, so the open-incident count
  // and the affected service surface without waiting for the interval.
  useEffect(() => {
    if (events.length && events.length !== lastLen.current) {
      lastLen.current = events.length
      load()
    }
  }, [events.length, load])

  const openIncidents = incidents.filter((i) => i.status !== "resolved")
  const freshness = useFreshness(updatedAt)

  const signal = (k: string, value: string, unit: string | null, detail: string, tone?: "alert" | "warn", series?: number[]) => (
    <div className={`sx-sig${tone ? ` ${tone}` : ""}`}>
      <div className="k">{k}</div>
      <div className="v">
        {value}
        {unit && <span className="u">{unit}</span>}
      </div>
      {series && series.length >= 2 && (
        <div style={{ margin: "4px 0 2px" }}>
          <Sparkline series={series} tone={tone === "alert" ? "crit" : tone === "warn" ? "warn" : "neutral"} width={104} height={20} />
        </div>
      )}
      <div className="d">{detail}</div>
    </div>
  )

  return (
    <ConsolePage title="Fleet overview" live={connected} updated={freshness}>
      {loading ? (
        <Spinner />
      ) : (
        <>
          {metricsErr && (
            <div className="sx-empty" style={{ borderColor: "var(--crit-t)", color: "var(--crit)", marginBottom: 14, textAlign: "left" }}>
              Prometheus is unreachable for this cluster — metrics below may be blank.{" "}
              <Link href={`/clusters/${id}/settings`} style={{ textDecoration: "underline" }}>Check connections →</Link>
            </div>
          )}
          <div className="sx-signals">
            {signal(
              "Error rate",
              metricsErr ? "—" : round(metrics?.errors ?? null, 2),
              metricsErr ? null : "%",
              metricsErr ? "prometheus unreachable" : "5xx / total · 5m",
              !metricsErr && (metrics?.errors ?? 0) >= 5 ? "alert" : (metrics?.errors ?? 0) >= 1 ? "warn" : undefined,
              hist.errors,
            )}
            {signal(
              "p95 latency",
              metricsErr ? "—" : round(metrics?.latency ?? null, 0),
              metricsErr ? null : "ms",
              "request duration · 5m",
              !metricsErr && (metrics?.latency ?? 0) >= 1000 ? "alert" : (metrics?.latency ?? 0) >= 500 ? "warn" : undefined,
              hist.latency,
            )}
            {signal(
              "CPU saturation",
              metricsErr ? "—" : round(metrics?.cpu ?? null, 1),
              metricsErr ? null : "%",
              "avg container · 5m",
              !metricsErr && (metrics?.cpu ?? 0) >= 85 ? "alert" : undefined,
              hist.cpu,
            )}
            {signal("Open incidents", String(openIncidents.length), null, openIncidents.length ? "needs attention" : "all clear", openIncidents.length ? "alert" : undefined)}
            {signal("MTTR · all time", analytics ? String(analytics.stats.mttr_minutes) : "—", analytics ? "min" : null, analytics ? `${analytics.stats.resolution_rate_pct}% resolved` : "no data")}
          </div>

          <SectionTitle
            title="Service health"
            meta={`${services.length} service${services.length === 1 ? "" : "s"}`}
            action={
              <Link href={`/clusters/${id}/services`} className="more">
                All services →
              </Link>
            }
          />
          {services.length === 0 ? (
            <Empty>
              No service metrics yet. Prometheus reports no service-labelled samples.{" "}
              <Link href={`/clusters/${id}/settings`} style={{ textDecoration: "underline" }}>Review connections &amp; metric names →</Link>
            </Empty>
          ) : (
            <table className="sx-tbl">
              <thead>
                <tr>
                  <th className="l">Service</th>
                  <th>Status</th>
                  <th>Req/s</th>
                  <th>Err %</th>
                  <th>p95</th>
                  <th>p99</th>
                </tr>
              </thead>
              <tbody>
                {services.map((s) => (
                  <tr key={s.name} className="click" onClick={() => (window.location.href = `/clusters/${id}/services/${s.name}`)}>
                    <td className="l">
                      <div className="sx-statc">
                        <span className={`sx-sq ${s.status}`} />
                        <span className="sx-svcn">
                          {s.name}
                          <small>{s.workload}</small>
                        </span>
                      </div>
                    </td>
                    <td>
                      <span className={`sx-badge ${s.status}`}>{s.status === "ok" ? "Healthy" : s.status === "warn" ? "Degraded" : "Critical"}</span>
                    </td>
                    <td>{round(s.rps, 1)}</td>
                    <td className={errCls(s.error_pct)}>{round(s.error_pct, 2)}</td>
                    <td className={(s.p95_ms ?? 0) >= 1000 ? "bad" : ""}>{s.p95_ms === null ? "—" : `${s.p95_ms}ms`}</td>
                    <td>{s.p99_ms === null ? "—" : `${s.p99_ms}ms`}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <div className="sx-two">
            <div>
              <SectionTitle
                title="Active incidents"
                action={
                  <Link href={`/clusters/${id}/incidents`} className="more">
                    All →
                  </Link>
                }
              />
              {openIncidents.length === 0 ? (
                <Empty>No open incidents. Sentinel is watching the telemetry.</Empty>
              ) : (
                openIncidents.map((i) => {
                  const sv = sev(i.severity)
                  const sb = statusBadge(i.status)
                  return (
                    <Link key={i.id} href={`/clusters/${id}/incidents/${i.id}`} className="sx-inc">
                      <div className={`sv ${sv.cls}`}>{sv.label}</div>
                      <div className="b2">
                        <div className="t">{i.title}</div>
                        <div className="m">
                          {i.id.slice(0, 8)} · opened {timeAgo(i.created_at)}
                        </div>
                      </div>
                      <div className="clock">
                        <small>{sb.label}</small>
                        {elapsed(i.created_at)}
                      </div>
                    </Link>
                  )
                })
              )}
            </div>

            <div>
              <SectionTitle title="Error budgets" meta="30-day" />
              {budgets.length === 0 ? (
                <Empty>No SLOs defined yet.</Empty>
              ) : (
                <div className="sx-bud">
                  {budgets.map((b) => (
                    <div className="row" key={b.slo.id}>
                      <div className="rt">
                        <span>{b.slo.name}</span>
                        <span className="p" style={{ color: `var(--${b.tone})` }}>
                          {b.remaining}%
                        </span>
                      </div>
                      <div className="sx-track">
                        <span style={{ width: `${b.remaining}%`, background: `var(--${b.tone})` }} />
                      </div>
                      <small>
                        target {b.slo.target}% · {b.slo.window_days}d window
                      </small>
                    </div>
                  ))}
                </div>
              )}

              <SectionTitle title="Recent changes" meta="audit" />
              {audit.length === 0 ? (
                <Empty>No recent activity.</Empty>
              ) : (
                <div className="sx-chg">
                  {audit.map((a) => (
                    <div className="c" key={a.id}>
                      <div className="mk">{timeAgo(a.timestamp)}</div>
                      <div>
                        <div className="ct">{a.action_type}</div>
                        <div className="cm">
                          {a.actor_id ?? a.actor_type ?? "system"}
                          {a.resource_target ? ` · ${a.resource_target}` : ""}
                          {a.outcome ? ` · ${a.outcome}` : ""}
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </>
      )}
    </ConsolePage>
  )
}
