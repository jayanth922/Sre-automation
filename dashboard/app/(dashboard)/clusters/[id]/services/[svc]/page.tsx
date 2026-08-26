"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import Link from "next/link"
import { useParams } from "next/navigation"
import { api } from "@/lib/auth-context"
import { useLiveStream } from "@/lib/useLiveStream"
import { ConsolePage } from "@/components/console/ConsolePage"
import { SectionTitle, Spinner, Empty, useFreshness } from "@/components/console/ui"
import { type ServiceHealth, type Incident, sev, statusBadge, timeAgo, elapsed, round } from "@/lib/console"

export default function ServiceDetailPage() {
  const { id, svc } = useParams<{ id: string; svc: string }>()
  const { events, connected } = useLiveStream(undefined, { channel: "incidents" })
  const [service, setService] = useState<ServiceHealth | null>(null)
  const [incidents, setIncidents] = useState<Incident[]>([])
  const [loading, setLoading] = useState(true)
  const [updatedAt, setUpdatedAt] = useState<number>(Date.now())
  const lastLen = useRef(0)

  const load = useCallback(async () => {
    const [s, inc] = await Promise.allSettled([
      api.get<ServiceHealth[]>(`/clusters/${id}/services`),
      api.get<Incident[]>(`/clusters/${id}/incidents`),
    ])
    if (s.status === "fulfilled") setService(s.value.data.find((x) => x.name === svc) ?? null)
    if (inc.status === "fulfilled") setIncidents(inc.value.data)
    setUpdatedAt(Date.now())
    setLoading(false)
  }, [id, svc])

  useEffect(() => {
    load()
    const t = setInterval(load, 12000)
    return () => clearInterval(t)
  }, [load])

  useEffect(() => {
    if (events.length && events.length !== lastLen.current) {
      lastLen.current = events.length
      load()
    }
  }, [events.length, load])

  const freshness = useFreshness(updatedAt)

  const related = incidents.filter((i) => `${i.title} ${i.description ?? ""}`.toLowerCase().includes(svc.toLowerCase()))

  const sig = (k: string, v: string, u: string | null, d: string, alert?: boolean) => (
    <div className={`sx-sig${alert ? " alert" : ""}`}>
      <div className="k">{k}</div>
      <div className="v">
        {v}
        {u && <span className="u">{u}</span>}
      </div>
      <div className="d">{d}</div>
    </div>
  )

  return (
    <ConsolePage
      crumb={
        <>
          <Link href={`/clusters/${id}/services`}>Services</Link> / {svc}
        </>
      }
      title={svc}
      live={connected}
      updated={freshness}
    >
      <Link href={`/clusters/${id}/services`} className="sx-back">
        ← Services
      </Link>

      {loading ? (
        <Spinner />
      ) : !service ? (
        <Empty>No Prometheus metrics for “{svc}”. It may not be scraped, or has no recent traffic.</Empty>
      ) : (
        <>
          <div className="sx-signals" style={{ gridTemplateColumns: "repeat(4,1fr)", marginTop: 12 }}>
            {sig("Req/s", round(service.rps, 1), null, "1m rate")}
            {sig("Error %", round(service.error_pct, 2), null, "5xx / total · 5m", service.status === "crit")}
            {sig("p95 latency", service.p95_ms === null ? "—" : String(service.p95_ms), service.p95_ms === null ? null : "ms", `p99 ${service.p99_ms ?? "—"}${service.p99_ms === null ? "" : "ms"}`, (service.p95_ms ?? 0) >= 1000)}
            {sig("Status", service.status === "ok" ? "Healthy" : service.status === "warn" ? "Degraded" : "Critical", null, service.workload, service.status === "crit")}
          </div>

          <div className="sx-two" style={{ marginTop: 8 }}>
            <div>
              <SectionTitle title="Related incidents" />
              {related.length === 0 ? (
                <Empty>No incidents reference this service.</Empty>
              ) : (
                related.map((i) => {
                  const sv = sev(i.severity)
                  const sb = statusBadge(i.status)
                  return (
                    <Link key={i.id} href={`/clusters/${id}/incidents/${i.id}`} className="sx-inc">
                      <div className={`sv ${sv.cls}`}>{sv.label}</div>
                      <div className="b2">
                        <div className="t">{i.title}</div>
                        <div className="m">opened {timeAgo(i.created_at)}</div>
                      </div>
                      <div className="clock">
                        <small>{sb.label}</small>
                        {elapsed(i.created_at, i.resolved_at)}
                      </div>
                    </Link>
                  )
                })
              )}
            </div>
            <div>
              <SectionTitle title="Metadata" />
              <div className="sx-card">
                <div className="sx-kv">
                  <span className="k">Workload</span>
                  <span className="v">{service.workload}</span>
                </div>
                <div className="sx-kv">
                  <span className="k">Status</span>
                  <span className="v">
                    <span className={`sx-badge ${service.status}`}>{service.status === "ok" ? "Healthy" : service.status === "warn" ? "Degraded" : "Critical"}</span>
                  </span>
                </div>
                <div className="sx-kv">
                  <span className="k">CPU</span>
                  <span className="v">{service.cpu_pct === null ? "—" : `${service.cpu_pct}%`}</span>
                </div>
                <div className="sx-kv">
                  <span className="k">Memory</span>
                  <span className="v">{service.mem_pct === null ? "—" : `${service.mem_pct}%`}</span>
                </div>
              </div>
            </div>
          </div>
        </>
      )}
    </ConsolePage>
  )
}
