"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import Link from "next/link"
import { useParams } from "next/navigation"
import { api, useAuth } from "@/lib/auth-context"
import { useLiveStream } from "@/lib/useLiveStream"
import { useCluster } from "@/components/console/ClusterContext"
import { ConsolePage } from "@/components/console/ConsolePage"
import { Spinner, useFreshness } from "@/components/console/ui"
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
  approval?: {
    approval_request_id: string
    action_hash: string
    expires_at: string
  } | null
}

interface GateApproval {
  id: string
  incident_id: string
  workflow_id: string
  gate: "start_fix" | "raise_pr" | "retry_fix" | "close_incident"
  status: "PENDING" | "APPROVED" | "REJECTED" | "EXPIRED"
  approver_user_id?: string | null
  decided_at?: string | null
  expires_at: string
  created_at: string
}

const GATE_LABEL: Record<GateApproval["gate"], string> = {
  start_fix: "Start fix in Temporal",
  raise_pr: "Raise pull request",
  retry_fix: "Retry the fix",
  close_incident: "Hand off to manual review",
}

interface AgentMetrics {
  nodes: Record<string, { runs: number; errors: number; total_ms: number; avg_ms: number }>
  provider_switches: { node: string; detail: string }[]
  total_runs: number
  total_errors: number
  total_ms: number
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
  const cluster = useCluster()
  const { events: liveEvents, connected } = useLiveStream(incidentId)
  const [tx, setTx] = useState<Transcript | null>(null)
  const [status, setStatus] = useState<GraphStatus | null>(null)
  const [agent, setAgent] = useState<AgentMetrics | null>(null)
  const [loading, setLoading] = useState(true)
  const [approving, setApproving] = useState(false)
  const [gates, setGates] = useState<GateApproval[]>([])
  const [updatedAt, setUpdatedAt] = useState<number>(Date.now())
  const lastLive = useRef(0)

  const loadTranscript = useCallback(async () => {
    try {
      const { data } = await api.get<Transcript>(`/incidents/${incidentId}/transcript`)
      setTx(data)
      setUpdatedAt(Date.now())
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

  const loadAgent = useCallback(async () => {
    try {
      const { data } = await api.get<AgentMetrics>(`/incidents/${incidentId}/agent-metrics`)
      setAgent(data)
    } catch {
      /* recorder may be empty */
    }
  }, [incidentId])

  const loadGates = useCallback(async () => {
    try {
      const { data } = await api.get<GateApproval[]>(`/incidents/${incidentId}/remediation-gates`)
      setGates(data)
    } catch {
      /* deterministic pipeline may not be in play for this incident */
    }
  }, [incidentId])

  useEffect(() => {
    loadTranscript()
    loadStatus()
    loadAgent()
    loadGates()
    const t = setInterval(() => {
      loadStatus()
      loadGates()
    }, 8000)
    return () => clearInterval(t)
  }, [loadTranscript, loadStatus, loadAgent, loadGates])

  // A new WebSocket frame for this incident → refetch canonical transcript.
  useEffect(() => {
    if (liveEvents.length && liveEvents.length !== lastLive.current) {
      lastLive.current = liveEvents.length
      loadTranscript()
      loadStatus()
      loadAgent()
      loadGates()
    }
  }, [liveEvents.length, loadTranscript, loadStatus, loadAgent, loadGates])

  const freshness = useFreshness(updatedAt)

  const approve = async () => {
    if (!status?.approval) return
    setApproving(true)
    try {
      await api.post(`/incidents/${incidentId}/approve`, {
        approval_request_id: status.approval.approval_request_id,
        action_hash: status.approval.action_hash,
      })
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
  const sb = statusBadge(inc.status, inc.summary)
  const events = tx.events
  const isAdmin = (user?.role ?? "member") === "admin"
  const awaitingApproval = status?.status === "WAITING_APPROVAL"

  // Concrete remediation actions from the act report on the timeline.
  type ActItem = { decision?: string; command?: string; rollback_command?: string; action_type?: string }
  const actReport = (() => {
    for (let i = events.length - 1; i >= 0; i--) {
      const p = events[i].payload as Record<string, unknown> | null
      if (p && p.act_report) return p.act_report as { executed?: ActItem[]; aggregate_decision?: string; summary?: string }
    }
    return null
  })()
  const actions: ActItem[] = actReport?.executed ?? []
  const pendingGates = gates.filter((g) => g.status === "PENDING")

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
      updated={freshness}
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
            {inc.jira_issue_key && (
              <a
                href={`${cluster?.jira_url ? cluster.jira_url.replace(/\/+$/, "") : ""}/browse/${inc.jira_issue_key}`}
                target="_blank"
                rel="noreferrer"
                className="sx-badge"
                style={{ textDecoration: "none" }}
              >
                {inc.jira_issue_key} ↗
              </a>
            )}
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
              if (ev.event_type === "trace_step") {
                const step = String(ev.payload?.step ?? ev.title ?? "step")
                const status = String(ev.payload?.status ?? "")
                const badgeCls =
                  status === "SUCCEEDED" ? "ok" : status === "FAILED" || status === "REFUSED" ? "crit"
                  : status === "STARTED" ? "sel" : "warn"
                const diff = typeof ev.payload?.diff === "string" ? ev.payload.diff : ""
                const logs = typeof ev.payload?.logs === "string" ? ev.payload.logs : ""
                const blob = diff || logs
                return (
                  <div className="sx-ev sx-trace" key={ev.id ?? ev.sequence ?? idx}>
                    <div className="rl">
                      <div className={`node trace ${status === "STARTED" ? "pulse" : ""}`} />
                      {idx < events.length - 1 && <div className="ln" />}
                    </div>
                    <div className="c2">
                      <div className="et">
                        {pretty(step)}
                        <span className={`sx-badge ${badgeCls}`} style={{ marginLeft: 8 }}>{status}</span>
                      </div>
                      {ev.content && <div className="ed">{ev.content}</div>}
                      {blob && (
                        <details>
                          <summary className="sx-tracesum">{diff ? "diff" : "logs"} ({blob.length} chars)</summary>
                          <pre className="sx-logbox">{blob}</pre>
                        </details>
                      )}
                      <div className="src">
                        <span className="sx-t2 tool">trace</span>
                        {String(ev.payload?.source ?? "executor")} · {timeAgo(ev.created_at)}
                      </div>
                    </div>
                  </div>
                )
              }
              const tag = sourceTag(ev)
              const isUser = ev.speaker_role === "user"
              const rawConf = typeof ev.payload?.confidence === "number" ? (ev.payload.confidence as number) : null
              const conf = rawConf === null ? null : Math.round(rawConf <= 1 ? rawConf * 100 : rawConf)
              const confidenceLabel = ev.payload?.confidence_calibrated === true
                ? "calibrated probability"
                : "self-reported confidence · uncalibrated"
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
                          {confidenceLabel}
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
            <div className="sx-dry" style={{ textAlign: "left", marginTop: 8, color: "var(--ink3)" }}>
              This conversation is read-only here. Reply in the incident's Slack thread to direct the investigation.
            </div>
          </div>

          {gates.length > 0 && (
            <div className="sx-remedy">
              <div className="h">⚙ Deterministic pipeline</div>
              {gates.map((g) => (
                <div className="sx-action" key={g.id} style={{ marginBottom: 10 }}>
                  <div className="at">
                    <span
                      className={`sx-badge ${
                        g.status === "APPROVED" ? "ok" : g.status === "PENDING" ? "warn" : "sel"
                      }`}
                    >
                      {g.status.toLowerCase()}
                    </span>
                    {GATE_LABEL[g.gate]}
                  </div>
                  {g.status === "PENDING" && (
                    <div className="ad">Expires {timeAgo(g.expires_at)}.</div>
                  )}
                </div>
              ))}
              {pendingGates.length > 0 && (
                <div className="sx-dry">Approve or deny from the incident's Slack thread ("approve {pendingGates[0].gate.replace(/_/g, "-")}" / "deny {pendingGates[0].gate.replace(/_/g, "-")}").</div>
              )}
            </div>
          )}

          {(actions.length > 0 || awaitingApproval) && (
            <div className="sx-remedy">
              <div className="h">⚙ Proposed remediation</div>
              {actions.length === 0 ? (
                <div className="sx-action">
                  <div className="at">
                    <span className="sx-badge sel">gated</span> Sentinel has a remediation ready
                  </div>
                  <div className="ad">Severity {sv.label}. Execution is paused pending approval.</div>
                </div>
              ) : (
                actions.map((a, i) => {
                  const autonomous = a.decision === "autonomous"
                  return (
                    <div className="sx-action" key={i} style={{ marginBottom: 10 }}>
                      <div className="at">
                        <span className={`sx-badge ${autonomous ? "ok" : "warn"}`}>{autonomous ? "autonomous" : "needs approval"}</span>
                        {a.action_type || "action"}
                      </div>
                      {a.command && <div className="ad">{a.command}</div>}
                      {a.rollback_command && <div className="gate" style={{ color: "var(--ink2)" }}>rollback: {a.rollback_command}</div>}
                    </div>
                  )
                })
              )}
              {actReport?.aggregate_decision && (
                <div className="sx-dry" style={{ textAlign: "left", marginTop: 8 }}>
                  Gate decision: {actReport.aggregate_decision} · dry-run verified
                </div>
              )}
              {awaitingApproval && (
                <>
                  <div className="sx-btnrow">
                    <button className="sx-btn primary" onClick={approve} disabled={!isAdmin || approving || !status?.approval}>
                      {approving ? "Approving…" : "Approve & run"}
                    </button>
                  </div>
                  {!isAdmin && <div className="sx-dry">Only admins can approve remediations.</div>}
                </>
              )}
            </div>
          )}

          {agent && agent.total_runs > 0 && (
            <div className="sx-remedy">
              <div className="h">◆ Agent run</div>
              <div className="sx-kv"><span className="k">Total agent time</span><span className="v">{(agent.total_ms / 1000).toFixed(1)}s</span></div>
              <div className="sx-kv"><span className="k">Reasoning steps</span><span className="v">{agent.total_runs}</span></div>
              {agent.total_errors > 0 && (
                <div className="sx-kv"><span className="k">Errors</span><span className="v" style={{ color: "var(--crit)" }}>{agent.total_errors}</span></div>
              )}
              {Object.entries(agent.nodes)
                .sort((a, b) => b[1].total_ms - a[1].total_ms)
                .slice(0, 5)
                .map(([name, n]) => (
                  <div className="sx-kv" key={name}>
                    <span className="k">{name}</span>
                    <span className="v">{n.runs}× · {(n.avg_ms / 1000).toFixed(1)}s avg</span>
                  </div>
                ))}
              {agent.provider_switches.length > 0 && (
                <div className="sx-dry" style={{ textAlign: "left", marginTop: 8 }}>
                  provider fallback: {agent.provider_switches.map((s) => s.detail).join(", ")}
                </div>
              )}
              <div className="sx-dry" style={{ textAlign: "left", marginTop: 8, color: "var(--ink3)" }}>
                Token &amp; cost accounting in Langfuse when enabled.
              </div>
            </div>
          )}
        </div>
      </div>
    </ConsolePage>
  )
}
