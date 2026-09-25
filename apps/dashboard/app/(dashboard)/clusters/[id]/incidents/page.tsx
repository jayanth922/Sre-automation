"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import Link from "next/link"
import { useParams } from "next/navigation"
import { api } from "@/lib/auth-context"
import { useLiveStream } from "@/lib/useLiveStream"
import { ConsolePage } from "@/components/console/ConsolePage"
import { Spinner, Empty, ErrorNote, useFreshness } from "@/components/console/ui"
import { type Incident, type Severity, sev, statusBadge, timeAgo, elapsed } from "@/lib/console"

type Tab = "open" | "all" | "resolved"
type SevFilter = "all" | Severity

/** Severity starts empty because the API refuses to guess one, and so should this. */
type TriggerForm = { title: string; description: string; severity: Severity | "" }
const EMPTY_TRIGGER: TriggerForm = { title: "", description: "", severity: "" }

/** `matchedExisting` separates "opened an investigation" from "found one already
 *  running", which the endpoint reports with the same response body. */
type TriggerResult = { incident: Incident; matchedExisting: boolean }

export default function IncidentsPage() {
  const { id } = useParams<{ id: string }>()
  // Live incident lifecycle feed — opens/resolves push here so the list reflects
  // them within ~1s instead of waiting for the fallback poll.
  const { events, connected } = useLiveStream(undefined, { channel: "incidents" })
  const [incidents, setIncidents] = useState<Incident[]>([])
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState(false)
  const [updatedAt, setUpdatedAt] = useState<number>(Date.now())
  const [tab, setTab] = useState<Tab>("open")
  const [q, setQ] = useState("")
  const [sevFilter, setSevFilter] = useState<SevFilter>("all")
  const lastLen = useRef(0)
  const [showTrigger, setShowTrigger] = useState(false)
  const [trigger, setTrigger] = useState<TriggerForm>(EMPTY_TRIGGER)
  const [triggering, setTriggering] = useState(false)
  const [triggerError, setTriggerError] = useState<string | null>(null)
  const [triggerResult, setTriggerResult] = useState<TriggerResult | null>(null)

  const load = useCallback(async () => {
    try {
      const { data } = await api.get<Incident[]>(`/clusters/${id}/incidents`)
      setIncidents(data)
      setErr(false)
      setUpdatedAt(Date.now())
    } catch {
      setErr(true)
    } finally {
      setLoading(false)
    }
  }, [id])

  useEffect(() => {
    load()
    // Live push covers real-time; the interval is just a slow safety net.
    const t = setInterval(load, 30000)
    return () => clearInterval(t)
  }, [load])

  // Refetch the instant an incident opens or resolves.
  useEffect(() => {
    if (events.length && events.length !== lastLen.current) {
      lastLen.current = events.length
      load()
    }
  }, [events.length, load])

  const open = incidents.filter((i) => i.status !== "resolved")
  const resolved = incidents.filter((i) => i.status === "resolved")
  const shown = tab === "open" ? open : tab === "resolved" ? resolved : incidents
  const ql = q.trim().toLowerCase()
  const filtered = shown.filter((i) => {
    if (sevFilter !== "all" && i.severity !== sevFilter) return false
    if (ql && !`${i.title} ${i.id} ${i.description ?? ""}`.toLowerCase().includes(ql)) return false
    return true
  })
  const filtersActive = ql !== "" || sevFilter !== "all"
  const freshness = useFreshness(updatedAt)

  const closeTrigger = () => {
    setShowTrigger(false)
    setTrigger(EMPTY_TRIGGER)
    setTriggerError(null)
  }

  const startInvestigation = async () => {
    const title = trigger.title.trim()
    const severity = trigger.severity
    if (!title) {
      setTriggerError("Give it a title. That title is the alert name the agent investigates.")
      return
    }
    if (!severity) {
      setTriggerError("Pick a severity. Nothing here assumes one for you.")
      return
    }
    setTriggering(true)
    setTriggerError(null)
    // Captured before the POST: an open incident with this exact title is
    // returned as-is rather than duplicated, and the response looks identical
    // either way. Whether we already knew the id is the only honest signal.
    const known = new Set(incidents.map((i) => i.id))
    const postedAt = Date.now()
    try {
      const { data } = await api.post<Incident>(`/clusters/${id}/trigger`, {
        title,
        description: trigger.description.trim() || null,
        severity,
      })
      // The id check is exact but only as current as the last poll; the age
      // check catches one opened since. A minute of slack keeps clock skew
      // between browser and server from reading a new incident as an old one.
      const predatesThisRequest = +new Date(data.created_at) < postedAt - 60_000
      setTriggerResult({
        incident: data,
        matchedExisting: known.has(data.id) || predatesThisRequest,
      })
      closeTrigger()
      await load()
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      setTriggerError(detail ?? "Could not start the investigation. No incident was opened.")
    } finally {
      setTriggering(false)
    }
  }

  const triggerButton = (
    <button
      type="button"
      className="sx-btn"
      style={{ flex: "none", padding: "5px 10px", fontSize: 11.5, marginLeft: "auto" }}
      onClick={() => {
        if (showTrigger) {
          closeTrigger()
        } else {
          setTriggerError(null)
          setTriggerResult(null)
          setShowTrigger(true)
        }
      }}
    >
      {showTrigger ? "Cancel" : "Start investigation"}
    </button>
  )

  const triggerPanel = showTrigger && (
    <div className="sx-dry" style={{ textAlign: "left", marginBottom: 14 }}>
      <div style={{ display: "flex", flexDirection: "column", gap: 12, maxWidth: 460 }}>
        <div style={{ fontWeight: 500, fontSize: 12.5 }}>Start an investigation</div>
        <div>
          <label className="sx-label" htmlFor="trigger-title">What is wrong</label>
          <input
            id="trigger-title"
            className="sx-input"
            placeholder="e.g. Checkout p99 latency above 2s"
            value={trigger.title}
            onChange={(e) => setTrigger({ ...trigger, title: e.target.value })}
          />
          <small style={{ color: "var(--ink3)", fontSize: 10.5 }}>
            Reaches the agent as the alert name. If an incident with this exact title is
            already open, that one is handed back and no second investigation starts.
          </small>
        </div>
        <div>
          <span className="sx-label">Severity</span>
          <div className="sx-tabs" style={{ marginBottom: 0 }}>
            {(["critical", "high", "medium", "low"] as Severity[]).map((s) => (
              <button
                key={s}
                type="button"
                className={trigger.severity === s ? "on" : ""}
                onClick={() => setTrigger({ ...trigger, severity: s })}
              >
                {sev(s).label}
              </button>
            ))}
          </div>
          <small style={{ color: "var(--ink3)", fontSize: 10.5, display: "block", marginTop: 6 }}>
            No default — state what you actually observed. Severity drives the urgency the
            agent plans against and the approval it needs before it changes anything.
          </small>
        </div>
        <div>
          <label className="sx-label" htmlFor="trigger-desc">Context (optional)</label>
          <textarea
            id="trigger-desc"
            className="sx-input"
            rows={3}
            style={{ resize: "vertical", fontFamily: "inherit" }}
            placeholder="What you saw, and where."
            value={trigger.description}
            onChange={(e) => setTrigger({ ...trigger, description: e.target.value })}
          />
          <small style={{ color: "var(--ink3)", fontSize: 10.5 }}>
            The first paragraph becomes the alert summary. A line reading{" "}
            <code>{'Labels: {"service": "checkout-service"}'}</code> is parsed into alert
            labels, which is how the agent works out which service to look at.
          </small>
        </div>
        {triggerError && <div style={{ color: "var(--crit)", fontSize: 11 }}>{triggerError}</div>}
        <button
          type="button"
          className="sx-btn primary"
          style={{ flex: "none", padding: "7px 18px", alignSelf: "flex-start" }}
          onClick={startInvestigation}
          disabled={triggering}
        >
          {triggering ? "Starting…" : "Start investigation"}
        </button>
      </div>
    </div>
  )

  const triggerNote = triggerResult && (
    <div
      className="sx-empty"
      style={{ textAlign: "left", padding: 12, marginTop: 0, marginBottom: 12, fontSize: 12 }}
    >
      {triggerResult.matchedExisting
        ? "That title already had an open incident, so nothing new was created — the agent is on it already."
        : "Investigation started. The agent is working it now."}{" "}
      <Link href={`/clusters/${id}/incidents/${triggerResult.incident.id}`}>
        {triggerResult.incident.title} · {triggerResult.incident.id.slice(0, 8)}
      </Link>
    </div>
  )

  return (
    <ConsolePage title="Incidents" live={connected} updated={freshness}>
      {loading ? (
        <Spinner />
      ) : err ? (
        <ErrorNote>Couldn’t load incidents — the API may be unreachable. Retrying automatically.</ErrorNote>
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

          <div style={{ display: "flex", gap: 10, margin: "10px 0 6px", flexWrap: "wrap", alignItems: "center" }}>
            <input
              className="sx-input"
              style={{ maxWidth: 280 }}
              placeholder="Search title, service, or id…"
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
            <div className="sx-tabs" style={{ marginBottom: 0 }}>
              {(["all", "critical", "high", "medium", "low"] as SevFilter[]).map((s) => (
                <button key={s} className={sevFilter === s ? "on" : ""} onClick={() => setSevFilter(s)}>
                  {s === "all" ? "All sev" : sev(s).label}
                </button>
              ))}
            </div>
            {triggerButton}
          </div>

          {triggerPanel}
          {triggerNote}

          {filtered.length === 0 ? (
            <Empty>
              {filtersActive
                ? "No incidents match your filters."
                : tab === "open"
                  ? "No open incidents. Telemetry is quiet."
                  : "No incidents here."}
            </Empty>
          ) : (
            filtered
              .slice()
              .sort((a, b) => +new Date(b.created_at) - +new Date(a.created_at))
              .map((i) => {
                const sv = sev(i.severity)
                const sb = statusBadge(i.status, i.summary)
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
