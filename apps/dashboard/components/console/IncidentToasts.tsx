"use client"

import { useEffect, useRef, useState } from "react"
import { useParams, useRouter } from "next/navigation"
import { useLiveStream } from "@/lib/useLiveStream"

// Global, cross-page notifications. Mounted once in the cluster layout so a user
// on any page (Analytics, SLOs, Settings…) is told the moment an incident opens,
// needs their approval, or resolves — with one click to jump straight to it.

interface Toast {
  id: string
  // Which cluster this incident belongs to. Required, not optional: the
  // incidents channel is org-wide, and a toast that cannot say where it came
  // from is one this cluster's page must not render.
  clusterId: string
  kind: "opened" | "awaiting_approval" | "resolved"
  incidentId?: string
  title: string
  detail?: string
}

const META: Record<Toast["kind"], { tone: string; label: string; icon: string }> = {
  opened: { tone: "crit", label: "Incident opened", icon: "●" },
  awaiting_approval: { tone: "warn", label: "Needs your approval", icon: "⏸" },
  resolved: { tone: "ok", label: "Incident resolved", icon: "✓" },
}

export function IncidentToasts() {
  const { id } = useParams<{ id: string }>()
  const router = useRouter()
  const { events } = useLiveStream(undefined, { channel: "incidents" })
  const [toasts, setToasts] = useState<Toast[]>([])
  const lastLen = useRef(0)
  const timers = useRef<Record<string, ReturnType<typeof setTimeout>>>({})

  const dismiss = (tid: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== tid))
    if (timers.current[tid]) {
      clearTimeout(timers.current[tid])
      delete timers.current[tid]
    }
  }

  useEffect(() => {
    if (events.length <= lastLen.current) {
      lastLen.current = events.length
      return
    }
    const fresh = events.slice(lastLen.current)
    lastLen.current = events.length

    for (const ev of fresh) {
      const kind = ev.type as Toast["kind"]
      if (kind !== "opened" && kind !== "awaiting_approval" && kind !== "resolved") continue
      const p = (ev.payload ?? {}) as Record<string, unknown>
      // The incidents channel is org-scoped, so it carries every cluster this
      // organization owns. An event with no cluster_id cannot be placed, and
      // is dropped rather than shown — the same fail-closed rule the server
      // applies to unscoped events one layer up. Which cluster a toast belongs
      // to is decided here; whether it is *this* cluster is decided at render,
      // because this component lives in the cluster layout and survives
      // navigation between sibling clusters.
      const clusterId = typeof p.cluster_id === "string" ? p.cluster_id : undefined
      if (!clusterId) continue
      const incidentId = (ev.incident_id ?? (p.incident_id as string) ?? undefined) as string | undefined
      const title = (p.alert_name as string) || (p.summary as string) || "Incident"
      const tid = `${incidentId ?? "x"}:${kind}:${ev.ts}`
      const toast: Toast = {
        id: tid,
        clusterId,
        kind,
        incidentId,
        title,
        detail: p.summary as string | undefined,
      }
      setToasts((prev) => [...prev.filter((t) => t.id !== tid), toast].slice(-4))
      // Approval prompts linger; open/resolve auto-dismiss.
      if (kind !== "awaiting_approval") {
        timers.current[tid] = setTimeout(() => dismiss(tid), 9000)
      }
    }
  }, [events])

  useEffect(() => {
    const t = timers.current
    return () => {
      Object.values(t).forEach(clearTimeout)
    }
  }, [])

  // Only this cluster's incidents. A sibling's toast here would disclose its
  // alert name and summary, and clicking it would build a URL pairing this
  // cluster with that incident — a guaranteed 404. The filter lives here
  // rather than at ingest because this component is mounted in the cluster
  // layout, which Next reuses across sibling [id] routes: `id` changes under
  // a component that never unmounts, and queued toasts outlive the move.
  const mine = toasts.filter((t) => t.clusterId === id)
  if (mine.length === 0) return null

  return (
    <div className="sx-toasts" role="status" aria-live="polite">
      {mine.map((t) => {
        const m = META[t.kind]
        const go = () => {
          if (t.incidentId) router.push(`/clusters/${id}/incidents/${t.incidentId}`)
          dismiss(t.id)
        }
        return (
          <div key={t.id} className={`sx-toast ${m.tone}`}>
            <span className="ic" aria-hidden>{m.icon}</span>
            <button className="body" onClick={go} title={t.incidentId ? "Open incident" : undefined}>
              <div className="lbl">{m.label}</div>
              <div className="ttl">{t.title}</div>
            </button>
            <button className="x" onClick={() => dismiss(t.id)} aria-label="Dismiss">×</button>
          </div>
        )
      })}
    </div>
  )
}
