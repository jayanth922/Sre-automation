"use client"

import { useCallback, useEffect, useState } from "react"
import Link from "next/link"
import { useParams } from "next/navigation"
import { api } from "@/lib/auth-context"
import { useLiveStream } from "@/lib/useLiveStream"
import { ConsolePage } from "@/components/console/ConsolePage"
import { Spinner, Empty } from "@/components/console/ui"
import { type Incident, sev, statusBadge, timeAgo, elapsed } from "@/lib/console"

type Tab = "open" | "all" | "resolved"

export default function IncidentsPage() {
  const { id } = useParams<{ id: string }>()
  const { connected } = useLiveStream()
  const [incidents, setIncidents] = useState<Incident[]>([])
  const [loading, setLoading] = useState(true)
  const [tab, setTab] = useState<Tab>("open")

  const load = useCallback(async () => {
    try {
      const { data } = await api.get<Incident[]>(`/clusters/${id}/incidents`)
      setIncidents(data)
    } finally {
      setLoading(false)
    }
  }, [id])

  useEffect(() => {
    load()
    const t = setInterval(load, 15000)
    return () => clearInterval(t)
  }, [load])

  const open = incidents.filter((i) => i.status !== "resolved")
  const resolved = incidents.filter((i) => i.status === "resolved")
  const shown = tab === "open" ? open : tab === "resolved" ? resolved : incidents

  return (
    <ConsolePage crumb="prod cluster" title="Incidents" live={connected}>
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

          {shown.length === 0 ? (
            <Empty>{tab === "open" ? "No open incidents. Telemetry is quiet." : "No incidents here."}</Empty>
          ) : (
            shown
              .slice()
              .sort((a, b) => +new Date(b.created_at) - +new Date(a.created_at))
              .map((i) => {
                const sv = sev(i.severity)
                const sb = statusBadge(i.status)
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
