"use client"

import { useMemo, useState } from "react"
import { useParams } from "next/navigation"
import { api } from "@/lib/auth-context"
import { useLiveStream } from "@/lib/useLiveStream"
import { ConsolePage } from "@/components/console/ConsolePage"
import { SectionTitle, Empty, ErrorNote } from "@/components/console/ui"
import { errCls, round, timeAgo, type NLQueryResult } from "@/lib/console"

interface HealthService {
  name: string
  status: "ok" | "warn" | "crit"
  error_pct: number | null
  p95_ms: number | null
  rps: number | null
}
interface HealthSnapshot {
  kind: string
  cluster: string
  cluster_id: string
  ts: string
  services: HealthService[]
  unhealthy: string[]
}

export default function InsightsPage() {
  const { id } = useParams<{ id: string }>()
  const { events, connected } = useLiveStream()
  const [q, setQ] = useState("")
  const [asking, setAsking] = useState(false)
  const [answer, setAnswer] = useState<NLQueryResult | null>(null)
  const [askErr, setAskErr] = useState(false)

  // Latest health snapshot for this cluster, plus a recent feed.
  const snapshots = useMemo(() => {
    return events
      .map((e) => e.payload as unknown as HealthSnapshot)
      .filter((p) => p && p.kind === "cluster_health")
      .reverse()
  }, [events])
  const latest = snapshots.find((s) => s.cluster_id === id) ?? snapshots[0]

  const ask = async () => {
    const question = q.trim()
    if (!question || asking) return
    setAsking(true)
    try {
      const { data } = await api.post<NLQueryResult>(`/clusters/${id}/query`, { question })
      setAnswer(data)
      setAskErr(false)
    } catch {
      setAskErr(true)
    } finally {
      setAsking(false)
    }
  }

  return (
    <ConsolePage title="Live insights" live={connected}>
      <SectionTitle title="Ask a metric question" meta="verified PromQL · read-only" />
      <div style={{ display: "flex", gap: 8, marginTop: 12, maxWidth: 640 }}>
        <input
          className="sx-input"
          aria-label="Ask a metric question"
          placeholder="e.g. show error rate for payment-service over 1h"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && ask()}
        />
        <button className="sx-btn primary" style={{ maxWidth: 120 }} onClick={ask} disabled={asking || !q.trim()}>
          {asking ? "Asking…" : "Ask"}
        </button>
      </div>
      {askErr && <ErrorNote>Couldn’t reach the query service — try again in a moment.</ErrorNote>}
      {answer && (
        <div className="sx-card" style={{ marginTop: 12, maxWidth: 640 }}>
          <div className="sx-mono" style={{ fontSize: 12, color: answer.valid ? "var(--ink)" : "var(--crit)" }}>
            {answer.promql || "(could not translate to a query)"}
          </div>
          {answer.error ? (
            <div style={{ marginTop: 8, fontSize: 12, color: "var(--crit)" }}>{answer.error}</div>
          ) : (
            <pre className="sx-mono" style={{ marginTop: 8, fontSize: 11, whiteSpace: "pre-wrap", color: "var(--ink2)" }}>
              {JSON.stringify(answer.data, null, 2)}
            </pre>
          )}
        </div>
      )}

      <SectionTitle title="Continuous health" meta={latest ? `updated ${timeAgo(latest.ts)}` : "waiting for monitor…"} />
      {!latest ? (
        <Empty>The monitor publishes a health snapshot each sweep. Nothing streamed yet.</Empty>
      ) : (
        <table className="sx-tbl">
          <thead>
            <tr>
              <th className="l">Service</th>
              <th>Status</th>
              <th>Req/s</th>
              <th>Err %</th>
              <th>p95</th>
            </tr>
          </thead>
          <tbody>
            {latest.services.map((s) => (
              <tr key={s.name}>
                <td className="l">
                  <div className="sx-statc">
                    <span className={`sx-sq ${s.status}`} />
                    <span className="sx-svcn">{s.name}</span>
                  </div>
                </td>
                <td>
                  <span className={`sx-badge ${s.status}`}>{s.status === "ok" ? "Healthy" : s.status === "warn" ? "Degraded" : "Critical"}</span>
                </td>
                <td>{round(s.rps, 1)}</td>
                <td className={errCls(s.error_pct)}>{round(s.error_pct, 2)}</td>
                <td className={(s.p95_ms ?? 0) >= 1000 ? "bad" : ""}>{s.p95_ms === null ? "—" : `${s.p95_ms}ms`}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {snapshots.length > 1 && (
        <>
          <SectionTitle title="Recent sweeps" />
          <div className="sx-chg">
            {snapshots.slice(0, 12).map((s, i) => (
              <div className="c" key={i}>
                <div className="mk">{timeAgo(s.ts)}</div>
                <div>
                  <div className="ct">{s.cluster}</div>
                  <div className="cm">
                    {s.unhealthy.length ? `unhealthy: ${s.unhealthy.join(", ")}` : "all services healthy"}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </ConsolePage>
  )
}
