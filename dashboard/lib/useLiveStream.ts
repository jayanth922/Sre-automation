"use client"

import { useEffect, useRef, useState } from "react"

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

/**
 * Subscribe to a live stream. Pass an incident id for that incident's conversation,
 * or omit it for the global insights stream. Returns the rolling list of events
 * (newest last) and the live connection state. Auto-reconnects with backoff.
 */
export function useLiveStream(incidentId?: string, maxEvents = 200) {
    const [events, setEvents] = useState<LiveEvent[]>([])
    const [connected, setConnected] = useState(false)
    const wsRef = useRef<WebSocket | null>(null)

    useEffect(() => {
        let closed = false
        let retry = 0
        let timer: ReturnType<typeof setTimeout>

        const connect = () => {
            if (closed) return
            const path = incidentId ? `/ws/incidents/${incidentId}` : "/ws/insights"
            const ws = new WebSocket(wsUrl(path))
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
                retry += 1
                timer = setTimeout(connect, Math.min(1000 * 2 ** retry, 15000)) // backoff, cap 15s
            }
            ws.onerror = () => ws.close()
        }

        connect()
        return () => {
            closed = true
            clearTimeout(timer)
            wsRef.current?.close()
        }
    }, [incidentId, maxEvents])

    return { events, connected }
}
