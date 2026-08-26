"use client"

import { useState, type CSSProperties } from "react"
import Link from "next/link"
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

export default function LoginPage() {
  const { login } = useAuth()
  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")
  const [error, setError] = useState("")
  const [loading, setLoading] = useState(false)

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true)
    setError("")
    try {
      const formData = new URLSearchParams()
      formData.append("username", email)
      formData.append("password", password)
      const res = await fetch("/auth/token", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: formData,
      })
      if (!res.ok) throw new Error("Incorrect email or password.")
      const data = await res.json()
      login(data.access_token)
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed.")
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={shell}>
      <div style={{ width: "100%", maxWidth: 380 }}>
        <div className="sx-wordmark" style={{ fontSize: 22, marginBottom: 4 }}>
          <span className="tick" /> Sentinel
        </div>
        <h1 style={{ fontSize: 24, fontWeight: 600, margin: "18px 0 6px" }}>Sign in</h1>
        <p style={{ color: "var(--ink2)", fontSize: 13.5, marginTop: 0, marginBottom: 24 }}>
          The reliability console for your clusters.
        </p>

        <form onSubmit={handleLogin} style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <div>
            <label className="sx-label">Email</label>
            <input className="sx-input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
          </div>
          <div>
            <label className="sx-label">Password</label>
            <input className="sx-input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} required />
          </div>
          {error && (
            <div className="sx-empty" style={{ borderColor: "var(--crit-t)", color: "var(--crit)", padding: 12, textAlign: "left" }}>
              {error}
            </div>
          )}
          <button className="sx-btn primary" type="submit" disabled={loading} style={{ maxWidth: 160 }}>
            {loading ? "Signing in…" : "Sign in"}
          </button>
        </form>

        <div style={{ marginTop: 24, fontSize: 13, color: "var(--ink2)" }}>
          New here? <Link href="/register" style={{ textDecoration: "underline", color: "var(--ink)" }}>Create an account</Link>
        </div>
      </div>
    </div>
  )
}
