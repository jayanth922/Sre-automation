"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import Link from "next/link"
import { useParams } from "next/navigation"
import { api } from "@/lib/auth-context"
import { useLiveStream } from "@/lib/useLiveStream"
import { ConsolePage } from "@/components/console/ConsolePage"
import { Spinner, Empty, ErrorNote, Sparkline, useFreshness } from "@/components/console/ui"
import { type ServiceHealth, round, errCls } from "@/lib/console"

export default function ServicesPage() {
  const { id } = useParams<{ id: string }>()
  const { events, connected } = useLiveStream(undefined, { channel: "incidents" })
  const [services, setServices] = useState<ServiceHealth[]>([])
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState(false)
  const [updatedAt, setUpdatedAt] = useState<number>(Date.now())
  // Rolling per-service error% history so each row shows a live trend.
  const [hist, setHist] = useState<Record<string, number[]>>({})
  const lastLen = useRef(0)

  const load = useCallback(async () => {
    try {
      const { data } = await api.get<ServiceHealth[]>(`/clusters/${id}/services`)
      setServices(data)
      setErr(false)
      setUpdatedAt(Date.now())
      setHist((h) => {
        const next: Record<string, number[]> = {}
        for (const s of data) {
          next[s.name] = [...(h[s.name] ?? []), s.error_pct ?? 0].slice(-24)
        }
        return next
      })
    } catch {
      setErr(true)
    } finally {
      setLoading(false)
    }
  }, [id])

  useEffect(() => {
    load()
    const t = setInterval(load, 12000)
    return () => clearInterval(t)
  }, [load])

  // An incident opening/resolving usually means a service just changed health —
  // refresh right away so the red service shows without waiting for the interval.
  useEffect(() => {
    if (events.length && events.length !== lastLen.current) {
      lastLen.current = events.length
      load()
    }
  }, [events.length, load])

  const freshness = useFreshness(updatedAt)

  return (
    <ConsolePage title="Services" live={connected} updated={freshness}>
      {loading ? (
        <Spinner />
      ) : err ? (
        <ErrorNote>Prometheus is unreachable for this cluster, so per-service signals aren’t available.</ErrorNote>
      ) : services.length === 0 ? (
        <Empty>No service-labelled metrics found in Prometheus.</Empty>
      ) : (
        <table className="sx-tbl">
          <thead>
            <tr>
              <th className="l">Service</th>
              <th>Status</th>
              <th>Req/s</th>
              <th>Err %</th>
              <th className="l">Err trend</th>
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
                <td className="l">
                  {(hist[s.name]?.length ?? 0) >= 2 ? (
                    <Sparkline series={hist[s.name]} tone={s.status === "crit" ? "crit" : s.status === "warn" ? "warn" : "neutral"} width={80} height={18} />
                  ) : (
                    <span style={{ color: "var(--ink3)" }}>—</span>
                  )}
                </td>
                <td className={(s.p95_ms ?? 0) >= 1000 ? "bad" : ""}>{s.p95_ms === null ? "—" : `${s.p95_ms}ms`}</td>
                <td>{s.p99_ms === null ? "—" : `${s.p99_ms}ms`}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </ConsolePage>
  )
}
