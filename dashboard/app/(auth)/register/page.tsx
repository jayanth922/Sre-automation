"use client"

import { useState, type CSSProperties } from "react"
import { useRouter } from "next/navigation"
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

export default function RegisterPage() {
  const router = useRouter()
  const [form, setForm] = useState({ email: "", password: "", fullName: "", organizationName: "" })
  const [error, setError] = useState("")
  const [loading, setLoading] = useState(false)

  const set = (k: string, v: string) => setForm((f) => ({ ...f, [k]: v }))

  const handleRegister = async (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true)
    setError("")
    try {
      const res = await fetch("/auth/register", {
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
        throw new Error(data.detail || "Registration failed.")
      }
      router.push("/login")
    } catch (err) {
      setError(err instanceof Error ? err.message : "Registration failed.")
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={shell}>
      <div style={{ width: "100%", maxWidth: 420 }}>
        <div className="sx-wordmark" style={{ fontSize: 22, marginBottom: 4 }}>
          <span className="tick" /> Sentinel
        </div>
        <h1 style={{ fontSize: 24, fontWeight: 600, margin: "18px 0 6px" }}>Create your account</h1>
        <p style={{ color: "var(--ink2)", fontSize: 13.5, marginTop: 0, marginBottom: 24, lineHeight: 1.6 }}>
          The first person to register an organization becomes its admin. Others join by registering with the same organization name.
        </p>

        <form onSubmit={handleRegister} style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <div>
            <label className="sx-label" htmlFor="reg-org">Organization name</label>
            <input id="reg-org" className="sx-input" placeholder="sjsu" value={form.organizationName} onChange={(e) => set("organizationName", e.target.value)} required />
          </div>
          <div>
            <label className="sx-label" htmlFor="reg-name">Full name</label>
            <input id="reg-name" className="sx-input" placeholder="optional" value={form.fullName} onChange={(e) => set("fullName", e.target.value)} />
          </div>
          <div>
            <label className="sx-label" htmlFor="reg-email">Email</label>
            <input id="reg-email" className="sx-input" type="email" value={form.email} onChange={(e) => set("email", e.target.value)} required />
          </div>
          <div>
            <label className="sx-label" htmlFor="reg-password">Password</label>
            <input id="reg-password" className="sx-input" type="password" minLength={8} value={form.password} onChange={(e) => set("password", e.target.value)} required />
          </div>
          {error && (
            <div className="sx-empty" style={{ borderColor: "var(--crit-t)", color: "var(--crit)", padding: 12, textAlign: "left" }}>
              {error}
            </div>
          )}
          <button className="sx-btn primary" type="submit" disabled={loading} style={{ maxWidth: 180 }}>
            {loading ? "Creating…" : "Create account"}
          </button>
        </form>

        <div style={{ marginTop: 24, fontSize: 13, color: "var(--ink2)" }}>
          Already have an account? <Link href="/login" style={{ textDecoration: "underline", color: "var(--ink)" }}>Sign in</Link>
        </div>
      </div>
    </div>
  )
}
