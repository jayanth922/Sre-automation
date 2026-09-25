"use client"

import { createContext, useCallback, useContext, useEffect, useState } from "react"
import { api, useAuth } from "@/lib/auth-context"

/**
 * The emergency lock (break glass), as the console sees it.
 *
 * `locked === null` means "could not find out" and is deliberately not the same
 * value as `false`. The backend read fails open — `is_cluster_locked` returns
 * False when Redis is unavailable — so a console that collapsed the two would
 * report the break glass as off at exactly the moment nobody can say. The
 * endpoint sends `state_available` alongside so the two stay apart here.
 */
interface LockState {
  locked: boolean | null
  stateAvailable: boolean
  refresh: () => Promise<void>
}

const LockContext = createContext<LockState>({
  locked: null,
  stateAvailable: false,
  refresh: async () => {},
})

export function useClusterLock(): LockState {
  return useContext(LockContext)
}

export function LockProvider({
  clusterId,
  children,
}: {
  clusterId: string
  children: React.ReactNode
}) {
  const [locked, setLocked] = useState<boolean | null>(null)
  const [stateAvailable, setStateAvailable] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const { data } = await api.get<{ locked: boolean; state_available?: boolean }>(
        `/clusters/${clusterId}/lock`,
      )
      // An older API that does not send the field is treated as available:
      // absent is not the same as explicitly false.
      const available = data.state_available !== false
      setStateAvailable(available)
      setLocked(available ? data.locked : null)
    } catch {
      setStateAvailable(false)
      setLocked(null)
    }
  }, [clusterId])

  useEffect(() => {
    // `refresh` is async: every setState in it runs after an await, so none is
    // the synchronous cascading render this rule guards against.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refresh()
    // Nothing pushes the lock, and another admin — or a direct API call — can
    // set it, so asking on a timer is the only way the banner stays honest.
    const t = setInterval(refresh, 10000)
    return () => clearInterval(t)
  }, [refresh])

  return (
    <LockContext.Provider value={{ locked, stateAvailable, refresh }}>
      {children}
    </LockContext.Provider>
  )
}

/** Shown on every cluster page while the lock is engaged.
 *
 * Only `locked === true` renders. An unknown state is reported by the control
 * on the Safety tab, not shouted from a banner on every page. */
export function BreakGlassBanner() {
  const { locked } = useClusterLock()
  if (locked !== true) return null
  return (
    <div
      role="alert"
      style={{
        marginBottom: 14,
        padding: "10px 14px",
        borderRadius: 2,
        border: "1px solid var(--crit)",
        background: "var(--crit-t)",
        color: "var(--crit)",
        fontSize: 12,
        lineHeight: 1.6,
      }}
    >
      <b>Emergency lock engaged.</b> Every automated remediation on this cluster is being
      rejected. Investigations still run and still report — nothing will be applied until an
      admin releases the lock from Settings → Safety.
    </div>
  )
}

/** The break-glass control itself. Admin-only, and it acts immediately. */
export function BreakGlassControl({ clusterId }: { clusterId: string }) {
  const { locked, stateAvailable, refresh } = useClusterLock()
  const { user } = useAuth()
  const isAdmin = (user?.role ?? "member") === "admin"
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [confirming, setConfirming] = useState(false)

  const apply = async (next: boolean) => {
    setBusy(true)
    setErr(null)
    try {
      await api.post(`/clusters/${clusterId}/lock`, { locked: next })
      await refresh()
      setConfirming(false)
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      // The lock is unchanged on any failure — the backend 500s rather than
      // half-applying — and saying so is the whole point of the message.
      setErr(detail ?? "Could not reach the lock. It is unchanged.")
    } finally {
      setBusy(false)
    }
  }

  const engaged = locked === true

  return (
    <div style={{ maxWidth: 620 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10 }}>
        <span className={`sx-badge ${engaged ? "crit" : locked === false ? "ok" : "warn"}`}>
          {engaged ? "ENGAGED" : locked === false ? "RELEASED" : "UNKNOWN"}
        </span>
        <b style={{ fontSize: 13 }}>Emergency lock · break glass</b>
      </div>

      <p style={{ fontSize: 12, color: "var(--ink2)", lineHeight: 1.7, margin: "0 0 12px" }}>
        While the lock is engaged the mutation gateway refuses every action the agent proposes
        for this cluster, whatever its severity and whoever approved it. Investigation,
        diagnosis and reporting are unaffected — the agent keeps working and keeps telling you
        what it would do. Use it when you would rather the platform did nothing at all than the
        wrong thing. Toggling it takes effect at once and is written to the audit trail.
      </p>

      {!stateAvailable && (
        <div
          className="sx-empty"
          style={{
            textAlign: "left",
            padding: 12,
            marginTop: 0,
            marginBottom: 12,
            borderColor: "var(--warn)",
            color: "var(--warn)",
            fontSize: 12,
          }}
        >
          The lock state store is unreachable, so the console cannot say whether the lock is set.
          Mutations are being rejected regardless: the gateway refuses to act on a lock it cannot
          read.
        </div>
      )}

      {err && (
        <div
          className="sx-empty"
          style={{
            textAlign: "left",
            padding: 12,
            marginTop: 0,
            marginBottom: 12,
            borderColor: "var(--crit-t)",
            color: "var(--crit)",
            fontSize: 12,
          }}
        >
          {err}
        </div>
      )}

      {confirming ? (
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span style={{ fontSize: 12, color: "var(--ink2)" }}>
            {engaged
              ? "Release the lock? The agent will be able to apply approved remediations again."
              : "Engage the lock? Every remediation on this cluster stops until it is released."}
          </span>
          <button
            className="sx-btn primary"
            style={{ flex: "none", padding: "6px 14px" }}
            onClick={() => apply(!engaged)}
            disabled={busy}
          >
            {busy ? "Working…" : engaged ? "Release" : "Engage"}
          </button>
          <button
            className="sx-btn"
            style={{ flex: "none", padding: "6px 14px" }}
            onClick={() => setConfirming(false)}
            disabled={busy}
          >
            Cancel
          </button>
        </div>
      ) : (
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <button
            className="sx-btn"
            style={{ flex: "none", padding: "6px 14px" }}
            onClick={() => setConfirming(true)}
            disabled={!isAdmin || locked === null}
          >
            {engaged ? "Release emergency lock" : "Engage emergency lock"}
          </button>
          {!isAdmin && (
            <span style={{ fontSize: 11, color: "var(--ink3)" }}>
              Only admins can pull the break glass.
            </span>
          )}
        </div>
      )}
    </div>
  )
}
