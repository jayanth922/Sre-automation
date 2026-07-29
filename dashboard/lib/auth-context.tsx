"use client"

import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react"
import { useRouter } from "next/navigation"
import axios from "axios"

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface AuthUser {
  user_id: string
  email: string
  org_id: string
  role: string
}

interface AuthContextValue {
  user: AuthUser | null
  token: string | null
  ready: boolean
  login: (token: string) => void
  logout: () => void
}

// ---------------------------------------------------------------------------
// JWT decode (no validation — the server validates; we only read claims)
// ---------------------------------------------------------------------------

function decodeJwt(token: string): AuthUser | null {
  try {
    const payload = token.split(".")[1]
    if (!payload) return null
    const json = atob(payload.replace(/-/g, "+").replace(/_/g, "/"))
    const claims = JSON.parse(json) as Record<string, unknown>
    return {
      user_id: String(claims.user_id ?? claims.sub ?? ""),
      email: String(claims.email ?? claims.sub ?? ""),
      org_id: String(claims.org_id ?? ""),
      role: String(claims.role ?? "member"),
    }
  } catch {
    return null
  }
}

// ---------------------------------------------------------------------------
// Access token lives in memory only (never localStorage → no XSS token theft).
// The refresh token is an httpOnly cookie the browser can't read.
// ---------------------------------------------------------------------------

let accessToken: string | null = null

export const api = axios.create({ baseURL: "/api/v1", withCredentials: true })

api.interceptors.request.use((config) => {
  if (accessToken) {
    config.headers = config.headers ?? {}
    config.headers["Authorization"] = `Bearer ${accessToken}`
  }
  return config
})

// Single-flight refresh so concurrent 401s don't stampede /auth/refresh.
let refreshPromise: Promise<string | null> | null = null
function refreshAccessToken(): Promise<string | null> {
  if (!refreshPromise) {
    refreshPromise = axios
      .post("/auth/refresh", {}, { withCredentials: true })
      .then((r) => {
        accessToken = r.data.access_token as string
        return accessToken
      })
      .catch(() => {
        accessToken = null
        return null
      })
      .finally(() => {
        refreshPromise = null
      })
  }
  return refreshPromise
}

let onAuthFail: () => void = () => {}

api.interceptors.response.use(
  (r) => r,
  async (error) => {
    const original = error.config
    if (error.response?.status === 401 && original && !original._retry) {
      original._retry = true
      const t = await refreshAccessToken()
      if (t) {
        original.headers = original.headers ?? {}
        original.headers["Authorization"] = `Bearer ${t}`
        return api(original)
      }
      onAuthFail()
    }
    return Promise.reject(error)
  },
)

// ---------------------------------------------------------------------------
// Context
// ---------------------------------------------------------------------------

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const router = useRouter()
  const [user, setUser] = useState<AuthUser | null>(null)
  const [ready, setReady] = useState(false)

  useEffect(() => {
    onAuthFail = () => {
      accessToken = null
      setUser(null)
      if (typeof window !== "undefined" && !window.location.pathname.startsWith("/login")) {
        router.push("/login")
      }
    }
    // Bootstrap: the access token is in memory only, so on every load we try to
    // mint one from the refresh cookie. Failure just means "not logged in".
    refreshAccessToken()
      .then((t) => {
        if (t) setUser(decodeJwt(t))
      })
      .finally(() => setReady(true))
  }, [router])

  const login = useCallback(
    (newToken: string) => {
      accessToken = newToken
      setUser(decodeJwt(newToken))
      router.push("/")
    },
    [router],
  )

  const logout = useCallback(async () => {
    try {
      await axios.post("/auth/logout", {}, { withCredentials: true })
    } catch {
      /* best-effort */
    }
    accessToken = null
    setUser(null)
    router.push("/login")
  }, [router])

  const value = useMemo<AuthContextValue>(
    () => ({ user, token: accessToken, ready, login, logout }),
    [user, ready, login, logout],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>")
  return ctx
}
