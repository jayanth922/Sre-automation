"use client"

import { useCallback, useEffect, useState } from "react"
import { useParams } from "next/navigation"
import { api } from "@/lib/auth-context"
import { ConsolePage } from "@/components/console/ConsolePage"
import { SectionTitle, Spinner, Empty } from "@/components/console/ui"
import { type AuditEvent, fmtTimeUTC } from "@/lib/console"

export default function AuditPage() {
  const { id } = useParams<{ id: string }>()
  const [events, setEvents] = useState<AuditEvent[]>([])
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    try {
      const { data } = await api.get<AuditEvent[]>(`/clusters/${id}/audit`, { params: { limit: 100 } })
      setEvents(data)
    } finally {
      setLoading(false)
    }
  }, [id])

  useEffect(() => {
    load()
    const t = setInterval(load, 15000)
    return () => clearInterval(t)
  }, [load])

  return (
    <ConsolePage title="Audit trail">
      {loading ? (
        <Spinner />
      ) : events.length === 0 ? (
        <Empty>No audit events recorded for this cluster yet.</Empty>
      ) : (
        <>
          <SectionTitle title="Activity" meta={`${events.length} events`} />
          <table className="sx-tbl">
            <thead>
              <tr>
                <th className="l">Time UTC</th>
                <th className="l">Actor</th>
                <th className="l">Action</th>
                <th className="l">Target</th>
                <th>Outcome</th>
              </tr>
            </thead>
            <tbody>
              {events.map((a) => (
                <tr key={a.id}>
                  <td className="l" style={{ color: "var(--ink2)" }}>
                    {fmtTimeUTC(a.timestamp)}
                  </td>
                  <td className="l">
                    <span className="sx-badge sel">{a.actor_id ?? a.actor_type ?? "system"}</span>
                  </td>
                  <td className="l">{a.action_type}</td>
                  <td className="l" style={{ color: "var(--ink2)" }}>
                    {a.resource_target ?? "—"}
                  </td>
                  <td>
                    <span className={`sx-badge ${a.outcome === "FAILED" ? "crit" : a.outcome === "SUCCESS" ? "ok" : "neutral"}`}>{a.outcome ?? "—"}</span>
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
