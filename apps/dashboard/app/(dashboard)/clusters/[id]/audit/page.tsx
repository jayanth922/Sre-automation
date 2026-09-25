"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import { useParams } from "next/navigation"
import { api } from "@/lib/auth-context"
import { useLiveStream } from "@/lib/useLiveStream"
import { ConsolePage } from "@/components/console/ConsolePage"
import { SectionTitle, Spinner, Empty, ErrorNote, useFreshness } from "@/components/console/ui"
import { type AuditEvent, fmtTimeUTC } from "@/lib/console"

export default function AuditPage() {
  const { id } = useParams<{ id: string }>()
  // Audit rows are written as the agent acts, so an incident lifecycle event is a
  // reliable cue that new entries exist — refetch on it.
  const { events: liveEvents, connected } = useLiveStream(undefined, { channel: "incidents" })
  const [events, setEvents] = useState<AuditEvent[]>([])
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState(false)
  const [updatedAt, setUpdatedAt] = useState<number>(Date.now())
  const lastLen = useRef(0)

  const load = useCallback(async () => {
    try {
      const { data } = await api.get<AuditEvent[]>(`/clusters/${id}/audit`, { params: { limit: 100 } })
      setEvents(data)
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
    const t = setInterval(load, 30000)
    return () => clearInterval(t)
  }, [load])

  useEffect(() => {
    if (liveEvents.length && liveEvents.length !== lastLen.current) {
      lastLen.current = liveEvents.length
      load()
    }
  }, [liveEvents.length, load])

  const freshness = useFreshness(updatedAt)

  return (
    <ConsolePage title="Audit trail" live={connected} updated={freshness}>
      {loading ? (
        <Spinner />
      ) : err ? (
        <ErrorNote>Couldn’t load the audit trail — the API may be unreachable.</ErrorNote>
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
