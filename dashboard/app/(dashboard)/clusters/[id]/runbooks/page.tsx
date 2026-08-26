"use client"

import { useCallback, useEffect, useState } from "react"
import { useParams } from "next/navigation"
import { api } from "@/lib/auth-context"
import { ConsolePage } from "@/components/console/ConsolePage"
import { SectionTitle, Spinner, Empty } from "@/components/console/ui"
import { type Runbook } from "@/lib/console"

export default function RunbooksPage() {
  const { id } = useParams<{ id: string }>()
  const [runbooks, setRunbooks] = useState<Runbook[]>([])
  const [loading, setLoading] = useState(true)
  const [q, setQ] = useState("")

  const load = useCallback(async () => {
    try {
      const { data } = await api.get<Runbook[]>(`/clusters/${id}/runbooks`)
      setRunbooks(data)
    } finally {
      setLoading(false)
    }
  }, [id])

  useEffect(() => {
    load()
  }, [load])

  const shown = runbooks.filter((r) => `${r.title} ${r.service} ${r.incident_type} ${r.id}`.toLowerCase().includes(q.toLowerCase()))

  return (
    <ConsolePage crumb="prod cluster" title="Runbooks">
      {loading ? (
        <Spinner />
      ) : runbooks.length === 0 ? (
        <Empty>No runbooks found in the corpus.</Empty>
      ) : (
        <>
          <SectionTitle
            title="Catalog"
            meta={`${runbooks.length} documents`}
            action={<input className="sx-input" style={{ width: 200 }} placeholder="Filter runbooks…" value={q} onChange={(e) => setQ(e.target.value)} />}
          />
          <table className="sx-tbl">
            <thead>
              <tr>
                <th className="l">Runbook</th>
                <th className="l">Service</th>
                <th className="l">Incident type</th>
                <th className="l">Severity</th>
                <th className="l">ID</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((r) => (
                <tr key={r.id} className="click" onClick={() => (window.location.href = `/clusters/${id}/runbooks/${encodeURIComponent(r.id)}`)}>
                  <td className="l" style={{ fontWeight: 500 }}>
                    {r.title}
                  </td>
                  <td className="l" style={{ color: "var(--ink2)" }}>
                    {r.service}
                  </td>
                  <td className="l" style={{ color: "var(--ink2)" }}>
                    {r.incident_type}
                  </td>
                  <td className="l">{r.severity}</td>
                  <td className="l" style={{ color: "var(--ink3)" }}>
                    {r.id}
                  </td>
                </tr>
              ))}
              {shown.length === 0 && (
                <tr>
                  <td className="l" colSpan={5} style={{ color: "var(--ink3)" }}>
                    No runbooks match “{q}”.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </>
      )}
    </ConsolePage>
  )
}
