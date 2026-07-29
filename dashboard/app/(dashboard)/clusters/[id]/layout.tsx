"use client"

import { useEffect, useState } from "react"
import { useParams, useRouter } from "next/navigation"
import { Rail } from "@/components/console/Rail"
import { api } from "@/lib/auth-context"
import type { Cluster, Incident } from "@/lib/console"

export default function ClusterLayout({ children }: { children: React.ReactNode }) {
  const { id } = useParams<{ id: string }>()
  const router = useRouter()
  const [cluster, setCluster] = useState<Cluster | null>(null)
  const [openIncidents, setOpenIncidents] = useState(0)
  const [state, setState] = useState<"loading" | "ready" | "missing">("loading")

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

  useEffect(() => {
    if (state !== "ready") return
    let alive = true
    const poll = async () => {
      try {
        const { data } = await api.get<Incident[]>(`/clusters/${id}/incidents`)
        if (alive) setOpenIncidents(data.filter((i) => i.status !== "resolved").length)
      } catch {
        /* ignore */
      }
    }
    poll()
    const t = setInterval(poll, 15000)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [id, state])

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
    if (typeof window !== "undefined") router.replace("/")
    return null
  }

  return (
    <div className="sx-app">
      <Rail cluster={cluster} openIncidents={openIncidents} />
      <main className="sx-main sx-scroll">{children}</main>
    </div>
  )
}
