"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import Link from "next/link"
import { useParams } from "next/navigation"
import { api } from "@/lib/auth-context"
import { useLiveStream } from "@/lib/useLiveStream"
import { ConsolePage } from "@/components/console/ConsolePage"
import { SectionTitle, Spinner, Empty, ErrorNote, useFreshness } from "@/components/console/ui"
import { cap, elapsed, timeAgo } from "@/lib/console"

interface RunManifest {
  id: string
  job_id: string
  incident_id: string
  schema_version: number
  manifest: Record<string, unknown>
  manifest_sha256: string
  comparable: boolean
  non_comparable_reasons: string[]
  root_trace_id: string
  created_at: string
}

interface Job {
  id: string
  cluster_id: string
  job_type: string
  status: string
  payload: string | null
  result: string | null
  logs: string | null
  created_at: string
  started_at: string | null
  completed_at: string | null
  run_manifest: RunManifest | null
  incident_id: string | null
  idempotency_key: string | null
  attempt_count: number | null
  max_attempts: number | null
  lease_owner: string | null
  lease_expires_at: string | null
  cancel_requested_at: string | null
  last_error: string | null
}

/** One flattened field the two manifests disagree about. */
interface Difference {
  path: string
  left: unknown
  right: unknown
}

interface Comparison {
  left_job_id: string
  right_job_id: string
  comparable: boolean
  non_comparable_reasons: string[]
  configuration_equal: boolean
  configuration_differences: Difference[]
  input_differences: Difference[]
}

// A job in one of these is still moving, so the list keeps polling. A list of
// finished jobs is static and polling it is pure noise.
const ACTIVE = new Set(["pending", "running"])

const STATUS_TONE: Record<string, string> = {
  completed: "ok",
  running: "sel",
  pending: "neutral",
  degraded: "warn",
  failed: "crit",
  dead_letter: "crit",
  cancelled: "neutral",
}

const short = (id: string) => id.slice(0, 8)
const words = (s: string) => s.replace(/_/g, " ")

function scalar(v: unknown): string {
  if (v === null || v === undefined) return "—"
  return typeof v === "string" ? v : JSON.stringify(v)
}

function detail(e: unknown, fallback: string): string {
  const d = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  return typeof d === "string" ? d : fallback
}

function DiffTable({ title, rows }: { title: string; rows: Difference[] }) {
  if (rows.length === 0) return null
  return (
    <>
      <SectionTitle title={title} meta={`${rows.length} differing ${rows.length === 1 ? "field" : "fields"}`} />
      <table className="sx-tbl">
        <thead>
          <tr>
            <th className="l">Path</th>
            <th className="l">Left</th>
            <th className="l">Right</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((d) => (
            <tr key={d.path}>
              <td className="l" style={{ fontFamily: "var(--font-mono), monospace" }}>{d.path}</td>
              <td className="l" style={{ color: "var(--ink2)" }}>{scalar(d.left)}</td>
              <td className="l">{scalar(d.right)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}

export default function JobsPage() {
  const { id } = useParams<{ id: string }>()
  // Jobs are created by the alert pipeline, so an incident lifecycle event is a
  // reliable cue that the list has changed.
  const { events: liveEvents, connected } = useLiveStream(undefined, { channel: "incidents" })
  const [jobs, setJobs] = useState<Job[]>([])
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState(false)
  const [updatedAt, setUpdatedAt] = useState<number>(Date.now())
  const lastLen = useRef(0)
  const [selected, setSelected] = useState<string | null>(null)
  const [cancelling, setCancelling] = useState<string | null>(null)
  const [actionErr, setActionErr] = useState<string | null>(null)
  const [against, setAgainst] = useState("")
  const [comparison, setComparison] = useState<Comparison | null>(null)
  const [comparing, setComparing] = useState(false)

  const load = useCallback(async () => {
    try {
      const { data } = await api.get<Job[]>(`/clusters/${id}/jobs`)
      setJobs(data)
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
  }, [load])

  const active = jobs.some((j) => ACTIVE.has(j.status))

  useEffect(() => {
    if (!active) return
    const t = setInterval(load, 10000)
    return () => clearInterval(t)
  }, [active, load])

  useEffect(() => {
    if (liveEvents.length && liveEvents.length !== lastLen.current) {
      lastLen.current = liveEvents.length
      load()
    }
  }, [liveEvents.length, load])

  const freshness = useFreshness(updatedAt)
  const job = jobs.find((j) => j.id === selected) ?? null
  const others = jobs.filter((j) => j.run_manifest && j.id !== selected)

  const select = (jobId: string) => {
    setSelected(jobId === selected ? null : jobId)
    setComparison(null)
    setAgainst("")
    setActionErr(null)
  }

  const cancel = async (jobId: string) => {
    setCancelling(jobId)
    setActionErr(null)
    try {
      await api.post(`/clusters/${id}/jobs/${jobId}/cancel`)
    } catch (e) {
      // A 409 usually means the worker finished it first; the refresh below
      // shows what actually happened either way.
      setActionErr(detail(e, "Could not request cancellation."))
    } finally {
      setCancelling(null)
      load()
    }
  }

  const compare = async () => {
    if (!job || !against) return
    setComparing(true)
    setActionErr(null)
    try {
      const { data } = await api.get<Comparison>(`/clusters/${id}/jobs/${job.id}/manifest/compare/${against}`)
      setComparison(data)
    } catch (e) {
      setActionErr(detail(e, "Could not compare these two runs."))
      setComparison(null)
    } finally {
      setComparing(false)
    }
  }

  return (
    <ConsolePage title="Jobs" live={connected} updated={freshness}>
      {loading ? (
        <Spinner />
      ) : err ? (
        <ErrorNote>Couldn’t load jobs — the API may be unreachable.</ErrorNote>
      ) : jobs.length === 0 ? (
        <Empty>
          No durable jobs for this cluster yet. Jobs are created by the alert pipeline when an
          incident is raised, not from the console.
        </Empty>
      ) : (
        <>
          <SectionTitle
            title="Durable jobs"
            meta={`${jobs.length} recorded${active ? " · one still running" : ""}`}
          />
          <table className="sx-tbl">
            <thead>
              <tr>
                <th className="l">Job</th>
                <th className="l">Status</th>
                <th className="l">Incident</th>
                <th>Attempts</th>
                <th>Started</th>
                <th>Duration</th>
                <th className="l">Manifest</th>
              </tr>
            </thead>
            <tbody>
              {jobs.map((j) => (
                <tr key={j.id} className="click" onClick={() => select(j.id)}>
                  <td className="l">
                    <div className="sx-svcn">
                      {words(j.job_type)}
                      <small>{short(j.id)}</small>
                    </div>
                  </td>
                  <td className="l">
                    <span className={`sx-badge ${STATUS_TONE[j.status] ?? "neutral"}`}>{words(j.status)}</span>
                    {j.cancel_requested_at && ACTIVE.has(j.status) && (
                      <span className="sx-badge warn" style={{ marginLeft: 6 }}>
                        cancelling
                      </span>
                    )}
                  </td>
                  <td className="l">
                    {j.incident_id ? (
                      <Link
                        href={`/clusters/${id}/incidents/${j.incident_id}`}
                        onClick={(e) => e.stopPropagation()}
                        style={{ color: "var(--ink)" }}
                      >
                        {short(j.incident_id)}
                      </Link>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td className={(j.attempt_count ?? 0) > 1 ? "warnc" : ""}>
                    {j.attempt_count ?? 0}/{j.max_attempts ?? 1}
                  </td>
                  <td style={{ color: "var(--ink2)" }}>{timeAgo(j.started_at ?? j.created_at)}</td>
                  <td>{j.started_at ? elapsed(j.started_at, j.completed_at) : "—"}</td>
                  <td className="l" style={{ color: "var(--ink3)" }}>
                    {j.run_manifest ? j.run_manifest.manifest_sha256.slice(0, 8) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {job && (
            <div className="sx-remedy">
              <div className="h">
                ◆ {words(job.job_type)} · {short(job.id)}
              </div>
              <div className="sx-kv">
                <span className="k">Status</span>
                <span className="v">{cap(words(job.status))}</span>
              </div>
              <div className="sx-kv">
                <span className="k">Created</span>
                <span className="v">{timeAgo(job.created_at)}</span>
              </div>
              {job.completed_at && (
                <div className="sx-kv">
                  <span className="k">Completed</span>
                  <span className="v">{timeAgo(job.completed_at)}</span>
                </div>
              )}
              {job.lease_owner && (
                <div className="sx-kv">
                  <span className="k">Lease</span>
                  <span className="v">
                    {job.lease_owner} · expires {timeAgo(job.lease_expires_at)}
                  </span>
                </div>
              )}
              {job.idempotency_key && (
                <div className="sx-kv">
                  <span className="k">Idempotency key</span>
                  <span className="v">{job.idempotency_key}</span>
                </div>
              )}
              {job.run_manifest ? (
                <>
                  <div className="sx-kv">
                    <span className="k">Manifest</span>
                    <span className="v">{job.run_manifest.manifest_sha256.slice(0, 16)}</span>
                  </div>
                  <div className="sx-kv">
                    <span className="k">Schema</span>
                    <span className="v">v{job.run_manifest.schema_version}</span>
                  </div>
                  {job.run_manifest.root_trace_id && (
                    <div className="sx-kv">
                      <span className="k">Root trace</span>
                      <span className="v">{job.run_manifest.root_trace_id}</span>
                    </div>
                  )}
                  <div className="sx-kv">
                    <span className="k">Comparable</span>
                    <span className="v">
                      <span className={`sx-badge ${job.run_manifest.comparable ? "ok" : "warn"}`}>
                        {job.run_manifest.comparable ? "yes" : "no"}
                      </span>
                    </span>
                  </div>
                  {job.run_manifest.non_comparable_reasons.length > 0 && (
                    <div className="sx-dry" style={{ textAlign: "left" }}>
                      {job.run_manifest.non_comparable_reasons.join(" · ")}
                    </div>
                  )}
                </>
              ) : (
                <div className="sx-dry" style={{ textAlign: "left" }}>
                  No run manifest — this job left no reproducibility record, so it can’t be
                  compared against another run.
                </div>
              )}
              {job.last_error && (
                <div className="sx-action" style={{ marginTop: 12 }}>
                  <div className="at">
                    <span className="sx-badge crit">last error</span>
                  </div>
                  <div className="ad">{job.last_error}</div>
                </div>
              )}
              {job.logs && <div className="sx-logbox">{job.logs}</div>}
              {(ACTIVE.has(job.status) || (job.run_manifest && others.length > 0)) && (
                <div className="sx-btnrow" style={{ alignItems: "center" }}>
                  {ACTIVE.has(job.status) && (
                    <button
                      className="sx-btn"
                      onClick={() => cancel(job.id)}
                      disabled={cancelling === job.id || !!job.cancel_requested_at}
                    >
                      {job.cancel_requested_at
                        ? "Cancellation requested"
                        : cancelling === job.id
                          ? "Requesting…"
                          : "Cancel job"}
                    </button>
                  )}
                  {job.run_manifest && others.length > 0 && (
                    <>
                      <select
                        className="sx-input"
                        value={against}
                        onChange={(e) => setAgainst(e.target.value)}
                        aria-label="Run to compare against"
                      >
                        <option value="">Compare against…</option>
                        {others.map((o) => (
                          <option key={o.id} value={o.id}>
                            {short(o.id)} · {words(o.job_type)} · {timeAgo(o.created_at)}
                          </option>
                        ))}
                      </select>
                      <button className="sx-btn" onClick={compare} disabled={!against || comparing}>
                        {comparing ? "Comparing…" : "Compare runs"}
                      </button>
                    </>
                  )}
                </div>
              )}
              {job.cancel_requested_at && (
                <div className="sx-dry" style={{ textAlign: "left" }}>
                  Cancellation requested {timeAgo(job.cancel_requested_at)}. The worker stops at its
                  next checkpoint — it is not killed mid-step.
                </div>
              )}
              {actionErr && (
                <div className="sx-dry" style={{ textAlign: "left", color: "var(--crit)" }}>
                  {actionErr}
                </div>
              )}
            </div>
          )}

          {comparison && (
            <div style={{ marginTop: 26 }}>
              <SectionTitle
                title="Run comparison"
                meta={`${short(comparison.left_job_id)} vs ${short(comparison.right_job_id)}`}
              />
              <div className="sx-kv">
                <span className="k">Configuration</span>
                <span className="v">
                  <span className={`sx-badge ${comparison.configuration_equal ? "ok" : "warn"}`}>
                    {comparison.configuration_equal
                      ? "identical"
                      : `${comparison.configuration_differences.length} differing`}
                  </span>
                </span>
              </div>
              <div className="sx-kv">
                <span className="k">Inputs</span>
                <span className="v">
                  <span className={`sx-badge ${comparison.input_differences.length === 0 ? "ok" : "sel"}`}>
                    {comparison.input_differences.length === 0
                      ? "identical"
                      : `${comparison.input_differences.length} differing`}
                  </span>
                </span>
              </div>
              {!comparison.comparable && (
                <div className="sx-dry" style={{ textAlign: "left" }}>
                  Not comparable
                  {comparison.non_comparable_reasons.length > 0
                    ? `: ${comparison.non_comparable_reasons.join(" · ")}`
                    : ""}
                  . The differences below are still exact, but these two runs were not produced
                  under equivalent conditions, so a difference in outcome proves nothing on its own.
                </div>
              )}
              {comparison.configuration_equal && comparison.input_differences.length === 0 && (
                <Empty>These two runs are identical in configuration and input.</Empty>
              )}
              <DiffTable title="Configuration drift" rows={comparison.configuration_differences} />
              <DiffTable title="Input drift" rows={comparison.input_differences} />
            </div>
          )}
        </>
      )}
    </ConsolePage>
  )
}
