"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import Link from "next/link"
import { useParams } from "next/navigation"
import { api, useAuth } from "@/lib/auth-context"
import { useLiveStream } from "@/lib/useLiveStream"
import { ConsolePage } from "@/components/console/ConsolePage"
import { Spinner } from "@/components/console/ui"
import {
  type Transcript,
  type TimelineEvent,
  sev,
  statusBadge,
  timeAgo,
  elapsed,
} from "@/lib/console"

interface GraphStatus {
  status: string
  next?: unknown
  values?: Record<string, unknown>
}

function sourceTag(ev: TimelineEvent): { cls: string; label: string } | null {
  const hay = `${ev.event_type} ${ev.title ?? ""} ${JSON.stringify(ev.payload ?? {})}`.toLowerCase()
  if (/prometheus|metric|latency|error rate|p95|p99/.test(hay)) return { cls: "metric", label: "metric" }
  if (/loki|\blog/.test(hay)) return { cls: "logs", label: "logs" }
  if (/k8s|kube|pod|deployment|replica|namespace/.test(hay)) return { cls: "k8s", label: "k8s" }
  if (/github|deploy|commit|revert|pull request/.test(hay)) return { cls: "deploy", label: "deploy" }
  if (/runbook/.test(hay)) return { cls: "book", label: "runbook" }
  if (/tool|query|call/.test(hay)) return { cls: "tool", label: "tool" }
  return null
}

function pretty(s: string): string {
  return s.replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())
}

export default function IncidentConsolePage() {
  const { id, incidentId } = useParams<{ id: string; incidentId: string }>()
  const { user } = useAuth()
  const { events: liveEvents, connected } = useLiveStream(incidentId)
  const [tx, setTx] = useState<Transcript | null>(null)
  const [status, setStatus] = useState<GraphStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [draft, setDraft] = useState("")
  const [sending, setSending] = useState(false)
  const [approving, setApproving] = useState(false)
  const lastLive = useRef(0)

  const loadTranscript = useCallback(async () => {
    try {
      const { data } = await api.get<Transcript>(`/incidents/${incidentId}/transcript`)
      setTx(data)
    } finally {
      setLoading(false)
    }
  }, [incidentId])

  const loadStatus = useCallback(async () => {
    try {
      const { data } = await api.get<GraphStatus>(`/incidents/${incidentId}/status`)
      setStatus(data)
    } catch {
      /* graph may not be started */
    }
  }, [incidentId])

  useEffect(() => {
    loadTranscript()
    loadStatus()
    const t = setInterval(loadStatus, 8000)
    return () => clearInterval(t)
  }, [loadTranscript, loadStatus])

  // A new WebSocket frame for this incident → refetch canonical transcript.
  useEffect(() => {
    if (liveEvents.length && liveEvents.length !== lastLive.current) {
      lastLive.current = liveEvents.length
      loadTranscript()
      loadStatus()
    }
  }, [liveEvents.length, loadTranscript, loadStatus])

  const sendMessage = async () => {
    const msg = draft.trim()
    if (!msg || sending) return
    setSending(true)
    try {
      await api.post(`/incidents/${incidentId}/message`, { message: msg })
      setDraft("")
      await loadTranscript()
    } finally {
      setSending(false)
    }
  }

  const approve = async () => {
    setApproving(true)
    try {
      await api.post(`/incidents/${incidentId}/approve`)
      await loadStatus()
    } finally {
      setApproving(false)
    }
  }

  if (loading) {
    return (
      <ConsolePage crumb="incidents" title="Incident" live={connected}>
        <Spinner />
      </ConsolePage>
    )
  }

  if (!tx) {
    return (
      <ConsolePage crumb="incidents" title="Incident not found" live={false}>
        <div className="sx-empty">This incident could not be loaded.</div>
      </ConsolePage>
    )
  }

  const inc = tx.incident
  const sv = sev(inc.severity)
  const sb = statusBadge(inc.status)
  const events = tx.events
  const isAdmin = (user?.role ?? "member") === "admin"
  const awaitingApproval = status?.status === "WAITING_APPROVAL"

  // Conversation for the side panel: user + assistant follow-ups.
  const chatEvents = events.filter((e) => e.speaker_role === "user" || e.event_type === "human_message" || /assistant|follow/.test(e.event_type))

  return (
    <ConsolePage
      crumb={
        <>
          <Link href={`/clusters/${id}/incidents`}>Incidents</Link> / {inc.id.slice(0, 8)}
        </>
      }
      title={inc.title}
      live={connected}
    >
      <Link href={`/clusters/${id}/incidents`} className="sx-back">
        ← Incidents
      </Link>

      <div className="sx-console">
        <div>
          {/* header line */}
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 14, flexWrap: "wrap" }}>
            <span className={`sx-badge ${sv.tone}`}>{sv.label}</span>
            <span className={`sx-badge ${sb.cls}`}>{sb.label}</span>
            <span className="sx-mono" style={{ fontSize: 12, color: "var(--ink2)" }}>
              opened {timeAgo(inc.created_at)} · {inc.resolved_at ? `resolved in ${elapsed(inc.created_at, inc.resolved_at)}` : `${elapsed(inc.created_at)} elapsed`}
            </span>
          </div>

          {inc.description && (
            <div className="sx-origin">
              <b>Origin signal.</b> {inc.description.split("\n\n")[0]}
              {" — opened by telemetry, not by hand."}
            </div>
          )}

          {/* timeline */}
          <div className="sx-phase" style={{ marginTop: 10 }}>
            <span className="lbl">Investigation</span>
            <span className="tag">{events.length} events · OODA</span>
            <span className="rule" />
          </div>

          {events.length === 0 ? (
            <div className="sx-empty">The agent hasn’t emitted investigation steps yet. This view updates live as it works.</div>
          ) : (
            events.map((ev, idx) => {
              const tag = sourceTag(ev)
              const isUser = ev.speaker_role === "user"
              const rawConf = typeof ev.payload?.confidence === "number" ? (ev.payload.confidence as number) : null
              const conf = rawConf === null ? null : Math.round(rawConf <= 1 ? rawConf * 100 : rawConf)
              return (
                <div className="sx-ev" key={ev.id ?? ev.sequence ?? idx}>
                  <div className="rl">
                    <div className={`node${isUser ? " user" : ""}`} />
                    {idx < events.length - 1 && <div className="ln" />}
                  </div>
                  <div className="c2">
                    <div className="et">
                      {ev.title || pretty(ev.event_type)}
                      {conf !== null && (
                        <span className="sx-conf">
                          confidence
                          <span className="sx-cbar">
                            <span style={{ width: `${conf}%` }} />
                          </span>
                          {conf}%
                        </span>
                      )}
                    </div>
                    {ev.content && <div className="ed">{ev.content}</div>}
                    <div className="src">
                      {tag && <span className={`sx-t2 ${tag.cls}`}>{tag.label}</span>}
                      {isUser ? "you" : ev.speaker_role || "agent"} · {timeAgo(ev.created_at)}
                    </div>
                  </div>
                </div>
              )
            })
          )}

          {tx.summary && (
            <div className="sx-rootc">
              <div className="h">◆ Root cause · Sentinel summary</div>
              <h3>{inc.title}</h3>
              <p>{tx.summary}</p>
            </div>
          )}
        </div>

        {/* right pane */}
        <div>
          <div className="sx-pane">
            <div className="sx-pane-h">
              <span className="tick" style={{ background: connected ? "var(--ok)" : "var(--ink3)" }} />
              Ask Sentinel
            </div>
            <div className="sx-chat sx-scroll">
              {chatEvents.length === 0 && !tx.summary && (
                <div className="sx-m2 a">
                  <div className="who2">Sentinel</div>
                  <div className="bub">Investigation in progress. Ask a question any time and I’ll fold it into the analysis.</div>
                </div>
              )}
              {tx.summary && chatEvents.length === 0 && (
                <div className="sx-m2 a">
                  <div className="who2">Sentinel</div>
                  <div className="bub">{tx.summary}</div>
                </div>
              )}
              {chatEvents.map((e, i) => (
                <div className={`sx-m2 ${e.speaker_role === "user" ? "u" : "a"}`} key={e.id ?? i}>
                  <div className="who2">{e.speaker_role === "user" ? "You" : "Sentinel"}</div>
                  <div className="bub">{e.content}</div>
                </div>
              ))}
            </div>
            <div className="sx-chatbox">
              <input
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && sendMessage()}
                placeholder="Direct the investigation…"
              />
              <button className="snd" onClick={sendMessage} disabled={sending || !draft.trim()}>
                ↑
              </button>
            </div>
          </div>

          {awaitingApproval && (
            <div className="sx-remedy">
              <div className="h">⚙ Awaiting approval</div>
              <div className="sx-action">
                <div className="at">
                  <span className="sx-badge sel">gated</span> Sentinel has a remediation ready
                </div>
                <div className="ad">Severity {sv.label}. Execution is paused pending human approval.</div>
                <div className="gate">⚠ requires admin approval before it runs</div>
              </div>
              <div className="sx-btnrow">
                <button className="sx-btn primary" onClick={approve} disabled={!isAdmin || approving}>
                  {approving ? "Approving…" : "Approve & run"}
                </button>
              </div>
              {!isAdmin && <div className="sx-dry">Only admins can approve remediations.</div>}
            </div>
          )}
        </div>
      </div>
    </ConsolePage>
  )
}
