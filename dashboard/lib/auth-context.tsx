"use client"

import React, {
    createContext,
    useCallback,
    useContext,
    useEffect,
    useMemo,
    useState,
} from "react"
import { useRouter } from "next/navigation"
import axios from "axios"
import Cookies from "js-cookie"

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
    login: (token: string) => void
    logout: () => void
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const TOKEN_KEY = "sre_token"
const COOKIE_KEY = "token"          // must match middleware.ts

/** Minimal JWT payload decoder — no validation, just base64url parsing. */
function decodeJwt(token: string): AuthUser | null {
    try {
        const payload = token.split(".")[1]
        if (!payload) return null
        const json = atob(payload.replace(/-/g, "+").replace(/_/g, "/"))
        const claims = JSON.parse(json) as Record<string, unknown>
        return {
            user_id: String(claims.sub ?? claims.user_id ?? ""),
            email: String(claims.email ?? ""),
            org_id: String(claims.org_id ?? ""),
            role: String(claims.role ?? "member"),
        }
    } catch {
        return null
    }
}

// ---------------------------------------------------------------------------
// Axios instance
// ---------------------------------------------------------------------------

/**
 * Pre-configured Axios instance. A request interceptor reads the token from
 * localStorage and injects it as a Bearer Authorization header so every call
 * made through `api` is automatically authenticated.
 */
export const api = axios.create({
    // All data endpoints live under /api/v1 on the backend.
    // The Next.js rewrite proxies /api/* → backend, so setting baseURL here
    // means api.get("/clusters") actually hits /api/v1/clusters.
    baseURL: "/api/v1",
})

api.interceptors.request.use((config) => {
    const token = localStorage.getItem(TOKEN_KEY)
    if (token) {
        config.headers = config.headers ?? {}
        config.headers["Authorization"] = `Bearer ${token}`
    }
    return config
})

// ---------------------------------------------------------------------------
// Context
// ---------------------------------------------------------------------------

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: React.ReactNode }) {
    const router = useRouter()
    const [token, setToken] = useState<string | null>(null)
    const [user, setUser] = useState<AuthUser | null>(null)

    // Rehydrate from localStorage on mount (client-side only)
    useEffect(() => {
        const stored = localStorage.getItem(TOKEN_KEY)
        if (stored) {
            const decoded = decodeJwt(stored)
            if (decoded) {
                setToken(stored)
                setUser(decoded)
            } else {
                localStorage.removeItem(TOKEN_KEY)
                Cookies.remove(COOKIE_KEY, { path: "/" })
            }
        }
    }, [])

    const login = useCallback((newToken: string) => {
        const decoded = decodeJwt(newToken)
        if (!decoded) {
            console.error("auth-context: received an invalid JWT — ignoring")
            return
        }
        localStorage.setItem(TOKEN_KEY, newToken)
        Cookies.set(COOKIE_KEY, newToken, { path: "/" })
        setToken(newToken)
        setUser(decoded)
        router.push("/")
    }, [router])

    const logout = useCallback(() => {
        localStorage.removeItem(TOKEN_KEY)
        Cookies.remove(COOKIE_KEY, { path: "/" })
        setToken(null)
        setUser(null)
        router.push("/login")
    }, [router])

    const value = useMemo<AuthContextValue>(
        () => ({ user, token, login, logout }),
        [user, token, login, logout],
    )

    return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export function useAuth(): AuthContextValue {
    const ctx = useContext(AuthContext)
    if (!ctx) {
        throw new Error("useAuth must be used inside <AuthProvider>")
    }
    return ctx
}
