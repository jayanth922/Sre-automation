"use client"

import { useEffect, useRef, useState } from "react"

import { api } from "./auth-context"

// Live incident/insight stream over WebSocket — the push replacement for polling.
// Connects to the agent runtime's /ws endpoints (see sre_agent/agent_runtime.py).
// The default is the browser's own origin, where Helm routes /ws straight to the
// API. Set NEXT_PUBLIC_WS_BASE only when the API is exposed on another origin.
// Reconnects pass the last durable cursor so the server can replay missed events.

export interface LiveEvent {
    type: string
    payload: Record<string, unknown>
    incident_id?: string | null
    ts: string
    v?: number
    org_id?: string | null
    id?: string
    cursor?: string | null
}

function wsUrl(path: string): string {
    const base = process.env.NEXT_PUBLIC_WS_BASE?.replace(/\/+$/, "")
    if (base) return `${base}${path}`
    if (typeof window === "undefined") return path
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:"
    return `${proto}//${window.location.host}${path}`
}

export type LiveChannel = "insights" | "incidents"

interface WsTicketResponse {
    ticket: string
    expires_in: number
}

/**
 * Subscribe to a live stream. Pass an incident id for that incident's conversation,
 * a channel ("incidents" for the cluster-wide incident lifecycle feed, "insights"
 * for global health insights), or nothing (defaults to insights). Returns the
 * rolling list of events (newest last) and the live connection state.
 * Auto-reconnects with backoff and resumes from the last event cursor.
 */
export function useLiveStream(incidentId?: string, opts?: { channel?: LiveChannel; maxEvents?: number }) {
    const channel: LiveChannel = opts?.channel ?? "insights"
    const maxEvents = opts?.maxEvents ?? 200
    const [events, setEvents] = useState<LiveEvent[]>([])
    const [connected, setConnected] = useState(false)
    const wsRef = useRef<WebSocket | null>(null)
    const cursorRef = useRef<string | null>(null)

    useEffect(() => {
        let closed = false
        let retry = 0
        let timer: ReturnType<typeof setTimeout> | undefined

        const scheduleReconnect = () => {
            if (closed) return
            retry += 1
            timer = setTimeout(() => { void connect() }, Math.min(1000 * 2 ** retry, 15000))
        }

        const connect = async () => {
            if (closed) return
            const path = incidentId
                ? `/ws/incidents/${incidentId}`
                : channel === "incidents"
                    ? "/ws/incidents"
                    : "/ws/insights"

            // A ticket is intentionally minted for every connection attempt,
            // including reconnects, so an expired ticket is never reused.
            let ticket: string
            try {
                const response = await api.post<WsTicketResponse>("/ws-tickets")
                ticket = response.data.ticket
            } catch {
                setConnected(false)
                scheduleReconnect()
                return
            }
            if (closed) return

            const baseUrl = wsUrl(path)
            const params = new URLSearchParams()
            params.set("ticket", ticket)
            if (cursorRef.current) params.set("cursor", cursorRef.current)
            const separator = baseUrl.includes("?") ? "&" : "?"
            const ws = new WebSocket(`${baseUrl}${separator}${params.toString()}`)
            wsRef.current = ws

            ws.onopen = () => {
                retry = 0
                setConnected(true)
            }
            ws.onmessage = (msg) => {
                try {
                    const event = JSON.parse(msg.data) as LiveEvent
                    if (event.cursor) cursorRef.current = event.cursor
                    setEvents((prev) => {
                        if (event.id && prev.some((existing) => existing.id === event.id)) {
                            return prev
                        }
                        return [...prev.slice(-(maxEvents - 1)), event]
                    })
                } catch {
                    /* ignore malformed frames */
                }
            }
            ws.onclose = () => {
                setConnected(false)
                if (closed) return
                scheduleReconnect()
            }
            ws.onerror = () => ws.close()
        }

        void connect()
        return () => {
            closed = true
            if (timer) clearTimeout(timer)
            wsRef.current?.close()
        }
    }, [incidentId, channel, maxEvents])

    return { events, connected }
}
