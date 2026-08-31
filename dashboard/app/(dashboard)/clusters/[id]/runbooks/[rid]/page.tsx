"use client"

import { useCallback, useEffect, useState } from "react"
import Link from "next/link"
import { useParams } from "next/navigation"
import { api } from "@/lib/auth-context"
import { ConsolePage } from "@/components/console/ConsolePage"
import { Spinner, Empty } from "@/components/console/ui"
import type { RunbookDetail } from "@/lib/console"

export default function RunbookDetailPage() {
  const { id, rid } = useParams<{ id: string; rid: string }>()
  const [rb, setRb] = useState<RunbookDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [notFound, setNotFound] = useState(false)

  const load = useCallback(async () => {
    try {
      const { data } = await api.get<RunbookDetail>(`/clusters/${id}/runbooks/${encodeURIComponent(rid)}`)
      setRb(data)
    } catch {
      setNotFound(true)
    } finally {
      setLoading(false)
    }
  }, [id, rid])

  useEffect(() => {
    load()
  }, [load])

  return (
    <ConsolePage
      crumb={
        <>
          <Link href={`/clusters/${id}/runbooks`}>Runbooks</Link> / {rid}
        </>
      }
      title={rb?.title ?? rid}
    >
      <Link href={`/clusters/${id}/runbooks`} className="sx-back">
        ← Runbooks
      </Link>

      {loading ? (
        <Spinner />
      ) : notFound || !rb ? (
        <Empty>Runbook "{rid}" not found in Notion.</Empty>
      ) : (
        <div style={{ maxWidth: 820 }}>
          <div className="sx-card" style={{ marginTop: 14, display: "flex", gap: 24, flexWrap: "wrap" }}>
            <div>
              <div className="sx-label">Service</div>
              <div className="sx-mono">{rb.service}</div>
            </div>
            <div>
              <div className="sx-label">Incident type</div>
              <div className="sx-mono">{rb.incident_type}</div>
            </div>
            <div>
              <div className="sx-label">Severity</div>
              <div className="sx-mono">{rb.severity}</div>
            </div>
            <div>
              <div className="sx-label">ID</div>
              <div className="sx-mono">{rb.id}</div>
            </div>
            {rb.path && rb.path.startsWith("http") && (
              <div>
                <div className="sx-label">Notion</div>
                <a href={rb.path} target="_blank" rel="noreferrer" className="sx-mono">
                  Open in Notion ↗
                </a>
              </div>
            )}
          </div>
          <div
            className="sx-card"
            style={{ marginTop: 16, whiteSpace: "pre-wrap", fontFamily: "var(--font-mono), monospace", fontSize: 12.5, lineHeight: 1.7, color: "var(--ink)" }}
          >
            {rb.content}
          </div>
        </div>
      )}
    </ConsolePage>
  )
}
