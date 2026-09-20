"use client"

import { useEffect, useState, type CSSProperties } from "react"
import { useRouter } from "next/navigation"
import { useAuth } from "@/lib/auth-context"

const shell: CSSProperties = {
  minHeight: "100vh",
  background: "var(--paper)",
  color: "var(--ink)",
  fontFamily: "var(--font-sans), 'Hanken Grotesk', sans-serif",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  padding: "48px 24px",
}

/**
 * Claiming a fresh install.
 *
 * Sentinel ships with no accounts and no configuration file. The first person
 * to open it creates the founding organisation and becomes its admin, and this
 * page then stops existing — which is why it checks before rendering anything.
 */
export default function SetupPage() {
  const router = useRouter()
  const { login } = useAuth()
  const [checking, setChecking] = useState(true)
  const [form, setForm] = useState({
    email: "",
    password: "",
    confirm: "",
    fullName: "",
    organizationName: "",
  })
  const [error, setError] = useState("")
  const [loading, setLoading] = useState(false)

  const set = (k: string, v: string) => setForm((f) => ({ ...f, [k]: v }))

  // An install is claimed exactly once. If someone already did it, this page
  // has nothing to offer and the sign-in form is the only useful destination.
  useEffect(() => {
    let cancelled = false
    fetch("/api/v1/setup/status")
      .then((res) => (res.ok ? res.json() : null))
      .then((status) => {
        if (cancelled) return
        if (status && !status.needs_setup) router.replace("/login")
        else setChecking(false)
      })
      .catch(() => {
        // Render the form anyway: the claim itself is authoritative and will
        // return 409 if the install turns out to be claimed already.
        if (!cancelled) setChecking(false)
      })
    return () => {
      cancelled = true
    }
  }, [router])

  const handleClaim = async (e: React.FormEvent) => {
    e.preventDefault()
    if (form.password !== form.confirm) {
      setError("The two passwords do not match.")
      return
    }
    setLoading(true)
    setError("")
    try {
      const res = await fetch("/api/v1/setup/claim", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: form.email,
          password: form.password,
          full_name: form.fullName,
          org_name: form.organizationName,
        }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || "Setup failed.")
      }
      const data = await res.json()
      // The claim signs you in, so there is no second trip through /login.
      login(data.access_token)
    } catch (err) {
      setError(err instanceof Error ? err.message : "Setup failed.")
    } finally {
      setLoading(false)
    }
  }

  if (checking) {
    return (
      <div style={shell}>
        <div style={{ color: "var(--ink2)", fontSize: 13.5 }}>Checking this installation…</div>
      </div>
    )
  }

  return (
    <div style={shell}>
      <div style={{ width: "100%", maxWidth: 460 }}>
        <div className="sx-wordmark" style={{ fontSize: 22, marginBottom: 4 }}>
          <span className="tick" /> Sentinel
        </div>
        <h1 style={{ fontSize: 24, fontWeight: 600, margin: "18px 0 6px" }}>Set up Sentinel</h1>
        <p style={{ color: "var(--ink2)", fontSize: 13.5, marginTop: 0, marginBottom: 20, lineHeight: 1.6 }}>
          Nobody has claimed this installation yet. Create the first account — it becomes the
          administrator of a new organisation, and everyone after you joins by invitation.
          Providers, Slack, clusters and the rest are configured in Settings afterwards; there is
          nothing to edit on disk.
        </p>

        <form onSubmit={handleClaim} style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <div>
            <label className="sx-label" htmlFor="setup-org">Organization name</label>
            <input
              id="setup-org"
              className="sx-input"
              placeholder="Platform Engineering"
              value={form.organizationName}
              onChange={(e) => set("organizationName", e.target.value)}
              required
            />
          </div>
          <div>
            <label className="sx-label" htmlFor="setup-name">Full name</label>
            <input
              id="setup-name"
              className="sx-input"
              placeholder="optional"
              value={form.fullName}
              onChange={(e) => set("fullName", e.target.value)}
            />
          </div>
          <div>
            <label className="sx-label" htmlFor="setup-email">Email</label>
            <input
              id="setup-email"
              className="sx-input"
              type="email"
              value={form.email}
              onChange={(e) => set("email", e.target.value)}
              required
            />
          </div>
          <div>
            <label className="sx-label" htmlFor="setup-password">Password</label>
            <input
              id="setup-password"
              className="sx-input"
              type="password"
              minLength={8}
              value={form.password}
              onChange={(e) => set("password", e.target.value)}
              required
            />
          </div>
          <div>
            <label className="sx-label" htmlFor="setup-confirm">Confirm password</label>
            <input
              id="setup-confirm"
              className="sx-input"
              type="password"
              minLength={8}
              value={form.confirm}
              onChange={(e) => set("confirm", e.target.value)}
              required
            />
          </div>

          <div
            className="sx-empty"
            style={{ padding: 12, textAlign: "left", fontSize: 12.5, lineHeight: 1.6, color: "var(--ink2)" }}
          >
            <strong style={{ color: "var(--ink)" }}>Back up your encryption keys.</strong> On first
            boot Sentinel generated a keystore in the <code>sentinel_keys</code> Docker volume. It
            is what decrypts the credentials you are about to save in Settings — back it up
            together with the database, because a database restored without it cannot read them.
          </div>

          {error && (
            <div
              className="sx-empty"
              style={{ borderColor: "var(--crit-t)", color: "var(--crit)", padding: 12, textAlign: "left" }}
            >
              {error}
            </div>
          )}

          <button className="sx-btn primary" type="submit" disabled={loading} style={{ maxWidth: 220 }}>
            {loading ? "Setting up…" : "Create admin account"}
          </button>
        </form>
      </div>
    </div>
  )
}
