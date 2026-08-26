"use client"

import Link from "next/link"
import { usePathname } from "next/navigation"
import { useAuth } from "@/lib/auth-context"
import { clusterStatusTone, type Cluster } from "@/lib/console"

interface RailProps {
  cluster: Cluster
  openIncidents: number
  awaitingApproval?: number
}

const MONITOR = [
  { n: "01", label: "Overview", seg: "" },
  { n: "02", label: "Services", seg: "services" },
  { n: "03", label: "Incidents", seg: "incidents" },
  { n: "04", label: "SLOs", seg: "slos" },
  { n: "05", label: "Live insights", seg: "insights" },
  { n: "06", label: "Analytics", seg: "analytics" },
]
const RECORDS = [
  { n: "07", label: "Runbooks", seg: "runbooks" },
  { n: "08", label: "Audit trail", seg: "audit" },
  { n: "09", label: "Team", seg: "team" },
  { n: "10", label: "Settings", seg: "settings" },
]

export function Rail({ cluster, openIncidents, awaitingApproval = 0 }: RailProps) {
  const pathname = usePathname()
  const { user, logout } = useAuth()
  const base = `/clusters/${cluster.id}`

  const isActive = (seg: string) => {
    if (seg === "") return pathname === base || pathname === `${base}/`
    return pathname === `${base}/${seg}` || pathname.startsWith(`${base}/${seg}/`)
  }

  const initials = (user?.email ?? "?").slice(0, 2).toUpperCase()

  const item = (it: { n: string; label: string; seg: string }) => (
    <Link
      key={it.seg || "overview"}
      href={it.seg ? `${base}/${it.seg}` : base}
      className={`sx-nav${isActive(it.seg) ? " on" : ""}`}
    >
      <span className="n">{it.n}</span>
      {it.label}
      {it.seg === "incidents" && awaitingApproval > 0 && (
        <span className="ct approve" title={`${awaitingApproval} awaiting your approval`}>{awaitingApproval} ⏸</span>
      )}
      {it.seg === "incidents" && openIncidents > 0 && <span className="ct">{openIncidents}</span>}
    </Link>
  )

  return (
    <aside className="sx-rail">
      <div className="sx-mast">
        <div className="sx-wordmark">
          <span className="tick" />
          Sentinel
          <small style={{ width: "100%" }}>Reliability console</small>
        </div>
      </div>

      <Link href="/" className="sx-env" title="Switch cluster">
        <span className="sq" style={{ background: clusterStatusTone(cluster.status) }} />
        <b>{cluster.name}</b>
        <i>▾</i>
      </Link>

      <div className="sx-navsec sx-sc">Monitor</div>
      <nav>{MONITOR.map(item)}</nav>

      <div className="sx-navsec sx-sc" style={{ marginTop: 14 }}>
        Records
      </div>
      <nav>{RECORDS.map(item)}</nav>

      <div className="sp" />

      <div className="sx-who">
        <div className="av">{initials}</div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontWeight: 500, fontSize: 12.5, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
            {user?.email ?? "—"}
          </div>
          <button
            onClick={logout}
            className="sx-mono"
            style={{ fontSize: 10.5, color: "var(--ink3)", background: "none", border: "none", padding: 0, cursor: "pointer" }}
          >
            {user?.role ?? "member"} · sign out
          </button>
        </div>
      </div>
    </aside>
  )
}
