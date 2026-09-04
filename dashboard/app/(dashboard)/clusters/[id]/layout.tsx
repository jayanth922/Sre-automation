"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import { useParams, useRouter } from "next/navigation"
import { Rail } from "@/components/console/Rail"
import { ClusterContext } from "@/components/console/ClusterContext"
import { IncidentToasts } from "@/components/console/IncidentToasts"
import { useLiveStream } from "@/lib/useLiveStream"
import { api } from "@/lib/auth-context"
import type { Cluster, Incident } from "@/lib/console"

export default function ClusterLayout({ children }: { children: React.ReactNode }) {
  const { id } = useParams<{ id: string }>()
  const router = useRouter()
  // Live incident feed so the rail's open-incident badge updates the instant one
  // opens or resolves, not on the next 15s poll.
  const { events } = useLiveStream(undefined, { channel: "incidents" })
  const [cluster, setCluster] = useState<Cluster | null>(null)
  const [openIncidents, setOpenIncidents] = useState(0)
  const [awaitingApproval, setAwaitingApproval] = useState(0)
  const [state, setState] = useState<"loading" | "ready" | "missing">("loading")
  const lastLen = useRef(0)

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const { data } = await api.get<Cluster[]>("/clusters")
        const found = data.find((c) => c.id === id)
        if (!alive) return
        if (!found) {
          setState("missing")
          return
        }
        setCluster(found)
        setState("ready")
      } catch {
        if (alive) setState("missing")
      }
    }
    load()
    return () => {
      alive = false
    }
  }, [id])

  const refreshCount = useCallback(async () => {
    try {
      const { data } = await api.get<Incident[]>(`/clusters/${id}/incidents`)
      setOpenIncidents(data.filter((i) => i.status !== "resolved").length)
    } catch {
      /* ignore */
    }
  }, [id])

  const refreshApproval = useCallback(async () => {
    try {
      const { data } = await api.get<{ count: number }>(`/clusters/${id}/incidents/awaiting-approval`)
      setAwaitingApproval(data.count ?? 0)
    } catch {
      /* ignore */
    }
  }, [id])

  useEffect(() => {
    if (state !== "ready") return
    refreshCount()
    refreshApproval()
    const t = setInterval(refreshCount, 15000)
    // Approval state has no push event, so poll it a bit more eagerly.
    const t2 = setInterval(refreshApproval, 10000)
    return () => {
      clearInterval(t)
      clearInterval(t2)
    }
  }, [state, refreshCount, refreshApproval])

  // Update badges immediately on any incident lifecycle event.
  useEffect(() => {
    if (state !== "ready") return
    if (events.length && events.length !== lastLen.current) {
      lastLen.current = events.length
      refreshCount()
      refreshApproval()
    }
  }, [events.length, state, refreshCount, refreshApproval])

  useEffect(() => {
    if (state === "missing") router.replace("/")
  }, [state, router])

  if (state === "loading") {
    return (
      <div className="sx-app">
        <div />
        <div className="sx-main">
          <div className="sx-spinner" />
        </div>
      </div>
    )
  }

  if (state === "missing" || !cluster) {
    return null
  }

  return (
    <ClusterContext.Provider value={cluster}>
      <div className="sx-app">
        <Rail cluster={cluster} openIncidents={openIncidents} awaitingApproval={awaitingApproval} />
        <main className="sx-main sx-scroll">{children}</main>
        <IncidentToasts />
      </div>
    </ClusterContext.Provider>
  )
}
