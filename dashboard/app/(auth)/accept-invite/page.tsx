"use client"

import { Suspense, useEffect, useState, type CSSProperties } from "react"
import { useSearchParams } from "next/navigation"
import Link from "next/link"

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

function AcceptInviteForm() {
  const params = useSearchParams()
  const [token, setToken] = useState("")
  const [form, setForm] = useState({ password: "", confirm: "", fullName: "" })
  const [error, setError] = useState("")
  const [loading, setLoading] = useState(false)
  const [done, setDone] = useState(false)

  const set = (k: string, v: string) => setForm((f) => ({ ...f, [k]: v }))

  // The token normally arrives in the link an administrator sends. There is no
  // email delivery, so it may equally reach the invitee as bare text — the
  // field stays editable either way.
  useEffect(() => {
    const fromUrl = params.get("token")
    if (fromUrl) setToken(fromUrl)
  }, [params])

  const handleAccept = async (e: React.FormEvent) => {
    e.preventDefault()
    if (form.password !== form.confirm) {
      setError("Those two passwords don't match.")
      return
    }
    setLoading(true)
    setError("")
    try {
      const res = await fetch("/api/v1/invitations/accept", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          token: token.trim(),
          password: form.password,
          full_name: form.fullName.trim() || null,
        }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        // A 422 answers with a list of field errors, not a sentence.
        throw new Error(
          typeof data.detail === "string"
            ? data.detail
            : "This invitation could not be accepted. Check the token and try again.",
        )
      }
      setDone(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : "This invitation could not be accepted.")
    } finally {
      setLoading(false)
    }
  }

  if (done) {
    return (
      <div style={shell}>
        <div style={{ width: "100%", maxWidth: 420 }}>
          <div className="sx-wordmark" style={{ fontSize: 22, marginBottom: 4 }}>
            <span className="tick" /> Sentinel
          </div>
          <h1 style={{ fontSize: 24, fontWeight: 600, margin: "18px 0 6px" }}>You&apos;re in</h1>
          <p style={{ color: "var(--ink2)", fontSize: 13.5, marginTop: 0, marginBottom: 24, lineHeight: 1.6 }}>
            Your account is ready. Sign in with the address the invitation was sent to and the
            password you just chose.
          </p>
          <Link href="/login" className="sx-btn primary" style={{ maxWidth: 140 }}>
            Sign in
          </Link>
        </div>
      </div>
    )
  }

  return (
    <div style={shell}>
      <div style={{ width: "100%", maxWidth: 420 }}>
        <div className="sx-wordmark" style={{ fontSize: 22, marginBottom: 4 }}>
          <span className="tick" /> Sentinel
        </div>
        <h1 style={{ fontSize: 24, fontWeight: 600, margin: "18px 0 6px" }}>Accept your invitation</h1>
        <p style={{ color: "var(--ink2)", fontSize: 13.5, marginTop: 0, marginBottom: 24, lineHeight: 1.6 }}>
          This joins the organization that invited you. Your email address and role come from the
          invitation itself — you only choose a password.
        </p>

        <form onSubmit={handleAccept} style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <div>
            <label className="sx-label" htmlFor="inv-token">Invitation token</label>
            <input
              id="inv-token"
              className="sx-input sx-mono"
              style={{ fontSize: 11.5 }}
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder="paste the token from your invitation"
              required
            />
          </div>
          <div>
            <label className="sx-label" htmlFor="inv-name">Full name</label>
            <input
              id="inv-name"
              className="sx-input"
              placeholder="optional"
              value={form.fullName}
              onChange={(e) => set("fullName", e.target.value)}
            />
          </div>
          <div>
            <label className="sx-label" htmlFor="inv-password">Password</label>
            <input
              id="inv-password"
              className="sx-input"
              type="password"
              minLength={8}
              value={form.password}
              onChange={(e) => set("password", e.target.value)}
              required
            />
          </div>
          <div>
            <label className="sx-label" htmlFor="inv-confirm">Confirm password</label>
            <input
              id="inv-confirm"
              className="sx-input"
              type="password"
              minLength={8}
              value={form.confirm}
              onChange={(e) => set("confirm", e.target.value)}
              required
            />
          </div>
          {error && (
            <div className="sx-empty" style={{ borderColor: "var(--crit-t)", color: "var(--crit)", padding: 12, textAlign: "left" }}>
              {error}
            </div>
          )}
          <button className="sx-btn primary" type="submit" disabled={loading} style={{ maxWidth: 180 }}>
            {loading ? "Joining…" : "Join organization"}
          </button>
        </form>

        <div style={{ marginTop: 24, fontSize: 13, color: "var(--ink2)" }}>
          Already have an account?{" "}
          <Link href="/login" style={{ textDecoration: "underline", color: "var(--ink)" }}>Sign in</Link>
        </div>
      </div>
    </div>
  )
}

export default function AcceptInvitePage() {
  // useSearchParams needs a boundary or the production build refuses to
  // prerender this route.
  return (
    <Suspense fallback={<div style={shell} />}>
      <AcceptInviteForm />
    </Suspense>
  )
}
