"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import Link from "next/link"
import { useParams } from "next/navigation"
import { api } from "@/lib/auth-context"
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

interface LiveResultItem {
  action_type?: string
  target?: string
  status?: string
  command?: string
  detail?: string
}

interface Verification {
  status?: string
  current_value?: number
  threshold?: number
  improvement_pct?: number
  detail?: string
}

interface ActReportPayload {
  live_results?: LiveResultItem[]
  executed?: LiveResultItem[]
  verification?: Verification
  aggregate_decision?: string
  summary?: string
}

interface GraphStatus {
  status: string
  next?: unknown
  values?: { act_report?: ActReportPayload } & Record<string, unknown>
  approval?: {
    approval_request_id: string
    action_hash: string
    expires_at: string
  } | null
}

// The POST answers with the issue key alone, so the panel refetches the GET,
// which assembles the browse URL from the cluster's Jira base.
interface Ticket {
  jira_configured: boolean
  jira_issue_key: string | null
  jira_issue_url: string | null
}

const LIVE_STATUS_TONE: Record<string, string> = {
  EXECUTED: "ok",
  SUCCESS: "ok",
  OK: "ok",
  REVERT_REQUESTED: "ok",
  REFUSED: "warn",
  DENIED: "warn",
  MANUAL_REQUIRED: "warn",
  DRY_RUN: "warn",
  ERROR: "crit",
  FAILED: "crit",
  FAILURE: "crit",
  UNHEALTHY: "crit",
}

function liveStatusTone(status?: string): string {
  return LIVE_STATUS_TONE[(status ?? "").toUpperCase()] ?? "sel"
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

// Investigation text from the agent is prose/markdown-ish, not chat — render
// paragraphs and bullet lists instead of dumping it as one unbroken blob.
function FormattedText({ text }: { text: string }) {
  const blocks = text.trim().split(/\n\s*\n/)
  return (
    <>
      {blocks.map((block, i) => {
        const lines = block.split("\n").map((l) => l.trim()).filter(Boolean)
        if (lines.length === 0) return null
        const bulleted = lines.every((l) => /^[-*•]\s+/.test(l))
        const numbered = lines.every((l) => /^\d+[.)]\s+/.test(l))
        if (lines.length > 1 && (bulleted || numbered)) {
          const items = lines.map((l) => l.replace(/^[-*•]\s+/, "").replace(/^\d+[.)]\s+/, ""))
          const List = numbered ? "ol" : "ul"
          return (
            <List key={i} className="sx-fmt-list">
              {items.map((it, j) => (
                <li key={j}>{it}</li>
              ))}
            </List>
          )
        }
        if (lines.length === 1 && /^#{1,4}\s+/.test(lines[0])) {
          return (
            <div key={i} className="sx-fmt-h">
              {lines[0].replace(/^#{1,4}\s+/, "")}
            </div>
          )
        }
        return (
          <p key={i} className="sx-fmt-p">
            {lines.join(" ")}
          </p>
        )
      })}
    </>
  )
}

export default function IncidentConsolePage() {
  const { id, incidentId } = useParams<{ id: string; incidentId: string }>()
  const cluster = useCluster()
  const { events: liveEvents, connected } = useLiveStream(incidentId)
  const [tx, setTx] = useState<Transcript | null>(null)
  const [status, setStatus] = useState<GraphStatus | null>(null)
  const [agent, setAgent] = useState<AgentMetrics | null>(null)
  const [loading, setLoading] = useState(true)
  const [gates, setGates] = useState<GateApproval[]>([])
  const [ticket, setTicket] = useState<Ticket | null>(null)
  const [ticketBusy, setTicketBusy] = useState(false)
  const [ticketErr, setTicketErr] = useState<string | null>(null)
  const [updatedAt, setUpdatedAt] = useState<number>(Date.now())
  const lastLive = useRef(0)

  const loadTranscript = useCallback(async () => {
    try {
      const { data } = await api.get<Transcript>(`/incidents/${incidentId}/transcript`)
      setTx(data)
      setUpdatedAt(Date.now())
    } catch {
      /* handled below via the !tx fallback, which covers both not-found and unreachable */
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

  const loadTicket = useCallback(async () => {
    try {
      const { data } = await api.get<Ticket>(`/clusters/${id}/incidents/${incidentId}/ticket`)
      setTicket(data)
    } catch {
      /* the panel stays hidden unless this cluster has Jira configured */
    }
  }, [id, incidentId])

  const createTicket = async () => {
    setTicketBusy(true)
    setTicketErr(null)
    try {
      await api.post(`/clusters/${id}/incidents/${incidentId}/ticket`, {
        severity: tx?.incident.severity,
      })
      await loadTicket()
    } catch (e) {
      const d = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
      setTicketErr(typeof d === "string" ? d : "Could not create the ticket.")
    } finally {
      setTicketBusy(false)
    }
  }

  useEffect(() => {
    loadTranscript()
    loadStatus()
    loadAgent()
    loadGates()
    loadTicket()
    const t = setInterval(() => {
      loadStatus()
      loadGates()
    }, 8000)
    return () => clearInterval(t)
  }, [loadTranscript, loadStatus, loadAgent, loadGates, loadTicket])

  // A new WebSocket frame for this incident → refetch canonical transcript.
  useEffect(() => {
    if (liveEvents.length && liveEvents.length !== lastLive.current) {
      lastLive.current = liveEvents.length
      loadTranscript()
      loadStatus()
      loadAgent()
      loadGates()
      loadTicket()
    }
  }, [liveEvents.length, loadTranscript, loadStatus, loadAgent, loadGates, loadTicket])

  const freshness = useFreshness(updatedAt)

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
  // human_message/assistant_message are chat turns — communication now
  // happens in the incident's Slack thread, so the investigation timeline
  // shows only the agent's actual OODA-loop artifacts (findings, decisions,
  // plans, actions, trace steps), not a conversation log.
  const CHAT_EVENT_TYPES = new Set(["human_message", "assistant_message"])
  const events = tx.events.filter((e) => !CHAT_EVENT_TYPES.has(e.event_type))
  const awaitingApproval = status?.status === "WAITING_APPROVAL"

  // Concrete remediation actions: prefer the live graph-state act_report
  // (current_state.values, updated the moment the Act node runs) over the
  // transcript-derived one, which only updates once a matching trace_step
  // event has been persisted and fetched.
  type ActItem = { decision?: string; command?: string; rollback_command?: string; action_type?: string; target?: string }
  const transcriptActReport = (() => {
    for (let i = events.length - 1; i >= 0; i--) {
      const p = events[i].payload as Record<string, unknown> | null
      if (p && p.act_report) return p.act_report as { executed?: ActItem[]; aggregate_decision?: string; summary?: string }
    }
    return null
  })()
  const liveActReport = status?.values?.act_report
  const liveResults: LiveResultItem[] = liveActReport?.live_results ?? []
  const normalizedTranscript: LiveResultItem[] = (transcriptActReport?.executed ?? []).map((a) => ({
    action_type: a.action_type,
    target: a.target,
    command: a.command,
    status: a.decision === "autonomous" ? "EXECUTED" : a.decision ? "MANUAL_REQUIRED" : undefined,
    detail: a.rollback_command ? `rollback: ${a.rollback_command}` : undefined,
  }))
  // Live graph-state results win whenever present — they're the freshest
  // signal (updated the instant the Act node runs), independent of the
  // transcript event stream.
  const actions: LiveResultItem[] = liveResults.length > 0 ? liveResults : normalizedTranscript
  const aggregateDecision = liveActReport?.aggregate_decision ?? transcriptActReport?.aggregate_decision
  const verification = liveActReport?.verification
  const pendingGates = gates.filter((g) => g.status === "PENDING")

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
                      {ev.content && <div className="ed"><FormattedText text={ev.content} /></div>}
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
              const rawConf = typeof ev.payload?.confidence === "number" ? (ev.payload.confidence as number) : null
              const conf = rawConf === null ? null : Math.round(rawConf <= 1 ? rawConf * 100 : rawConf)
              const confidenceLabel = ev.payload?.confidence_calibrated === true
                ? "calibrated probability"
                : "self-reported confidence · uncalibrated"
              return (
                <div className="sx-ev" key={ev.id ?? ev.sequence ?? idx}>
                  <div className="rl">
                    <div className="node" />
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
                    {ev.content && <div className="ed"><FormattedText text={ev.content} /></div>}
                    <div className="src">
                      {tag && <span className={`sx-t2 ${tag.cls}`}>{tag.label}</span>}
                      {ev.speaker_role || "agent"} · {timeAgo(ev.created_at)}
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
              <FormattedText text={tx.summary} />
            </div>
          )}
        </div>

        {/* right pane */}
        <div>
          <div className="sx-pane">
            <div className="sx-pane-h">
              <span className="tick" style={{ background: connected ? "var(--ok)" : "var(--ink3)" }} />
              Live execution
            </div>
            {tx.summary && <div className="sx-origin" style={{ marginBottom: 10 }}>{tx.summary}</div>}
            <div className="sx-dry" style={{ textAlign: "left", color: "var(--ink3)" }}>
              All approvals and communication happen in the incident's Slack thread. This panel just mirrors what the agent is doing right now.
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

          {inc.status === "pending_acknowledgment" && (
            <div className="sx-remedy">
              <div className="h">⚙ Awaiting acknowledgment</div>
              <div className="sx-action" style={{ marginBottom: 10 }}>
                <div className="at">
                  <span className="sx-badge warn">verified</span> Live verification confirmed the fix
                </div>
                <div className="ad">Not marked resolved yet — a human still needs to confirm it.</div>
              </div>
              <div className="sx-dry">Reply "acknowledge" in the incident's Slack thread to mark this resolved.</div>
            </div>
          )}

          {(actions.length > 0 || awaitingApproval) && (
            <div className="sx-remedy">
              <div className="h">⚙ {liveResults.length > 0 ? "Live execution" : "Proposed remediation"}</div>
              {actions.length === 0 ? (
                <div className="sx-action">
                  <div className="at">
                    <span className="sx-badge sel">gated</span> Sentinel has a remediation ready
                  </div>
                  <div className="ad">Severity {sv.label}. Execution is paused pending approval.</div>
                </div>
              ) : (
                actions.map((a, i) => {
                  const tone = liveStatusTone(a.status)
                  return (
                    <div className="sx-action" key={i} style={{ marginBottom: 10 }}>
                      <div className="at">
                        <span className={`sx-badge ${tone}`}>{(a.status || "pending").toLowerCase().replace(/_/g, " ")}</span>
                        {a.action_type || "action"}
                        {a.target && <span style={{ color: "var(--ink3)" }}> · {a.target}</span>}
                      </div>
                      {a.command && <div className="ad">{a.command}</div>}
                      {a.detail && <div className="gate" style={{ color: "var(--ink2)" }}>{a.detail}</div>}
                    </div>
                  )
                })
              )}
              {verification && (
                <div className="sx-action" style={{ marginBottom: 10 }}>
                  <div className="at">
                    <span className={`sx-badge ${liveStatusTone(verification.status)}`}>{(verification.status || "unknown").toLowerCase()}</span>
                    verification
                  </div>
                  {verification.current_value !== undefined && verification.threshold !== undefined && (
                    <div className="ad">
                      {verification.current_value} vs threshold {verification.threshold}
                      {verification.improvement_pct !== undefined && ` · ${verification.improvement_pct.toFixed(1)}% improvement`}
                    </div>
                  )}
                  {verification.detail && <div className="gate" style={{ color: "var(--ink2)" }}>{verification.detail}</div>}
                </div>
              )}
              {aggregateDecision && (
                <div className="sx-dry" style={{ textAlign: "left", marginTop: 8 }}>
                  Gate decision: {aggregateDecision} · dry-run verified
                </div>
              )}
              {awaitingApproval && (
                <div className="sx-dry">Reply "approve fix" in the incident's Slack thread to run it.</div>
              )}
            </div>
          )}

          {ticket?.jira_configured && (
            <div className="sx-remedy">
              <div className="h">◆ Jira</div>
              {ticket.jira_issue_key ? (
                <div className="sx-kv">
                  <span className="k">Issue</span>
                  <span className="v">
                    {ticket.jira_issue_url ? (
                      <a
                        href={ticket.jira_issue_url}
                        target="_blank"
                        rel="noreferrer"
                        style={{ color: "var(--ink)" }}
                      >
                        {ticket.jira_issue_key}
                      </a>
                    ) : (
                      ticket.jira_issue_key
                    )}
                  </span>
                </div>
              ) : (
                <>
                  <div className="sx-action" style={{ marginBottom: 10 }}>
                    <div className="at">
                      <span className="sx-badge sel">unlinked</span> No Jira issue for this incident
                    </div>
                    <div className="ad">
                      Sentinel opens one automatically for incidents raised after Jira was
                      configured. This one predates that, or the automatic open failed.
                    </div>
                  </div>
                  <div className="sx-btnrow">
                    <button className="sx-btn" onClick={createTicket} disabled={ticketBusy}>
                      {ticketBusy ? "Creating…" : "Create ticket"}
                    </button>
                  </div>
                </>
              )}
              {ticketErr && (
                <div className="sx-dry" style={{ textAlign: "left", color: "var(--crit)" }}>
                  {ticketErr}
                </div>
              )}
            </div>
          )}

          {agent && agent.total_runs > 0 && (
            <div className="sx-remedy">
              <div className="h">◆ Agent run</div>
              <div className="sx-kv"><span className="k">Recorded node time</span><span className="v">{(agent.total_ms / 1000).toFixed(1)}s</span></div>
              <div className="sx-kv"><span className="k">Graph node runs</span><span className="v">{agent.total_runs}</span></div>
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
