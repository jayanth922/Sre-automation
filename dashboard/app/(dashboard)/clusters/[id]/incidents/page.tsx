"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import Link from "next/link"
import { useParams } from "next/navigation"
import { api } from "@/lib/auth-context"
import { useLiveStream } from "@/lib/useLiveStream"
import { ConsolePage } from "@/components/console/ConsolePage"
import { Spinner, Empty, useFreshness } from "@/components/console/ui"
import { type Incident, type Severity, sev, statusBadge, timeAgo, elapsed } from "@/lib/console"

type Tab = "open" | "all" | "resolved"
type SevFilter = "all" | Severity

export default function IncidentsPage() {
  const { id } = useParams<{ id: string }>()
  // Live incident lifecycle feed — opens/resolves push here so the list reflects
  // them within ~1s instead of waiting for the fallback poll.
  const { events, connected } = useLiveStream(undefined, { channel: "incidents" })
  const [incidents, setIncidents] = useState<Incident[]>([])
  const [loading, setLoading] = useState(true)
  const [updatedAt, setUpdatedAt] = useState<number>(Date.now())
  const [tab, setTab] = useState<Tab>("open")
  const [q, setQ] = useState("")
  const [sevFilter, setSevFilter] = useState<SevFilter>("all")
  const lastLen = useRef(0)

  const load = useCallback(async () => {
    try {
      const { data } = await api.get<Incident[]>(`/clusters/${id}/incidents`)
      setIncidents(data)
      setUpdatedAt(Date.now())
    } finally {
      setLoading(false)
    }
  }, [id])

  useEffect(() => {
    load()
    // Live push covers real-time; the interval is just a slow safety net.
    const t = setInterval(load, 30000)
    return () => clearInterval(t)
  }, [load])

  // Refetch the instant an incident opens or resolves.
  useEffect(() => {
    if (events.length && events.length !== lastLen.current) {
      lastLen.current = events.length
      load()
    }
  }, [events.length, load])

  const open = incidents.filter((i) => i.status !== "resolved")
  const resolved = incidents.filter((i) => i.status === "resolved")
  const shown = tab === "open" ? open : tab === "resolved" ? resolved : incidents
  const ql = q.trim().toLowerCase()
  const filtered = shown.filter((i) => {
    if (sevFilter !== "all" && i.severity !== sevFilter) return false
    if (ql && !`${i.title} ${i.id} ${i.description ?? ""}`.toLowerCase().includes(ql)) return false
    return true
  })
  const filtersActive = ql !== "" || sevFilter !== "all"
  const freshness = useFreshness(updatedAt)

  return (
    <ConsolePage title="Incidents" live={connected} updated={freshness}>
      {loading ? (
        <Spinner />
      ) : (
        <>
          <div className="sx-tabs">
            <button className={tab === "open" ? "on" : ""} onClick={() => setTab("open")}>
              Open · {open.length}
            </button>
            <button className={tab === "all" ? "on" : ""} onClick={() => setTab("all")}>
              All · {incidents.length}
            </button>
            <button className={tab === "resolved" ? "on" : ""} onClick={() => setTab("resolved")}>
              Resolved · {resolved.length}
            </button>
          </div>

          <div style={{ display: "flex", gap: 10, margin: "10px 0 6px", flexWrap: "wrap", alignItems: "center" }}>
            <input
              className="sx-input"
              style={{ maxWidth: 280 }}
              placeholder="Search title, service, or id…"
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
            <div className="sx-tabs" style={{ marginBottom: 0 }}>
              {(["all", "critical", "high", "medium", "low"] as SevFilter[]).map((s) => (
                <button key={s} className={sevFilter === s ? "on" : ""} onClick={() => setSevFilter(s)}>
                  {s === "all" ? "All sev" : sev(s).label}
                </button>
              ))}
            </div>
          </div>

          {filtered.length === 0 ? (
            <Empty>
              {filtersActive
                ? "No incidents match your filters."
                : tab === "open"
                  ? "No open incidents. Telemetry is quiet."
                  : "No incidents here."}
            </Empty>
          ) : (
            filtered
              .slice()
              .sort((a, b) => +new Date(b.created_at) - +new Date(a.created_at))
              .map((i) => {
                const sv = sev(i.severity)
                const sb = statusBadge(i.status, i.summary)
                return (
                  <Link key={i.id} href={`/clusters/${id}/incidents/${i.id}`} className="sx-inc">
                    <div className={`sv ${sv.cls}`}>{sv.label}</div>
                    <div className="b2">
                      <div className="t">{i.title}</div>
                      <div className="m">
                        {i.id.slice(0, 8)} · opened {timeAgo(i.created_at)}
                        {i.resolved_at ? ` · resolved ${timeAgo(i.resolved_at)}` : ""}
                      </div>
                    </div>
                    <div className="clock">
                      <small>{sb.label}</small>
                      {elapsed(i.created_at, i.resolved_at)}
                    </div>
                  </Link>
                )
              })
          )}
        </>
      )}
    </ConsolePage>
  )
}
