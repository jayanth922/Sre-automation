"use client"

import { useEffect, useRef, useState } from "react"

import { api } from "./auth-context"

// Live incident/insight stream over WebSocket — the push replacement for polling.
// Connects to the agent runtime's /ws endpoints (see sre_agent/agent_runtime.py).
// Note: WebSocket upgrades may not pass through the Next.js rewrite proxy in all
// setups; set NEXT_PUBLIC_WS_BASE to the backend origin (ws://host:8080) if so.

export interface LiveEvent {
    type: string
    payload: Record<string, unknown>
    incident_id?: string | null
    ts: string
}

function wsUrl(path: string): string {
    const base = process.env.NEXT_PUBLIC_WS_BASE
    if (base) return `${base}${path}`
    if (typeof window === "undefined") return path
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:"
    // Default: proxy under /agent (configure a rewrite/ingress that preserves upgrades).
    return `${proto}//${window.location.host}/agent${path}`
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
 * Auto-reconnects with backoff.
 */
export function useLiveStream(incidentId?: string, opts?: { channel?: LiveChannel; maxEvents?: number }) {
    const channel: LiveChannel = opts?.channel ?? "insights"
    const maxEvents = opts?.maxEvents ?? 200
    const [events, setEvents] = useState<LiveEvent[]>([])
    const [connected, setConnected] = useState(false)
    const wsRef = useRef<WebSocket | null>(null)

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
            const separator = baseUrl.includes("?") ? "&" : "?"
            const ws = new WebSocket(`${baseUrl}${separator}ticket=${encodeURIComponent(ticket)}`)
            wsRef.current = ws

            ws.onopen = () => {
                retry = 0
                setConnected(true)
            }
            ws.onmessage = (msg) => {
                try {
                    const event = JSON.parse(msg.data) as LiveEvent
                    setEvents((prev) => [...prev.slice(-(maxEvents - 1)), event])
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
