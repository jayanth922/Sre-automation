"use client"

import { useMemo, useState } from "react"
import { Loader2, Send, Wifi, WifiOff } from "lucide-react"

import { Button } from "@/components/ui/button"
import { api } from "@/lib/auth-context"
import { useLiveStream, type LiveEvent } from "@/lib/useLiveStream"

// Live incident chat panel (design slice #4). Mirrors the war-room conversation
// over the same live bus as Slack: agent events stream in via WebSocket, and an
// operator reply POSTs to the incident /message endpoint — feeding the exact
// human-checkpoint queue the supervisor consumes. So Slack and the dashboard are
// two symmetric views of one conversation.

interface Props {
    incidentId: string
}

interface ChatLine {
    key: string
    title: string
    content: string
    role: string
}

function toLine(ev: LiveEvent, i: number): ChatLine | null {
    if (ev.type !== "timeline") return null
    const p = ev.payload as Record<string, unknown>
    const content = String(p.content ?? "").trim()
    if (!content) return null
    return {
        key: `${ev.ts}-${i}`,
        title: String(p.title ?? p.speaker_role ?? "Agent"),
        content,
        role: String(p.speaker_role ?? "agent"),
    }
}

function roleClass(role: string): string {
    switch (role) {
        case "supervisor":
            return "border-cyan-500/30 bg-cyan-500/5"
        case "executor":
            return "border-amber-500/30 bg-amber-500/5"
        case "user":
        case "human":
            return "border-zinc-600/40 bg-zinc-800/40"
        default:
            return "border-zinc-800 bg-zinc-950/50"
    }
}

export function IncidentChatPanel({ incidentId }: Props) {
    const { events, connected } = useLiveStream(incidentId)
    const [text, setText] = useState("")
    const [sending, setSending] = useState(false)
    const [error, setError] = useState<string | null>(null)

    const lines = useMemo(
        () => events.map(toLine).filter((l): l is ChatLine => l !== null),
        [events]
    )

    const send = async () => {
        const message = text.trim()
        if (!message) return
        setSending(true)
        setError(null)
        try {
            // Feeds the supervisor's human-checkpoint queue; the agent's reply
            // streams back into this same panel via the live bus.
            await api.post(`/incidents/${incidentId}/message`, { message })
            setText("")
        } catch (sendError) {
            setError(sendError instanceof Error ? sendError.message : "Failed to send")
        } finally {
            setSending(false)
        }
    }

    return (
        <div className="flex h-full min-h-0 flex-col rounded-2xl border border-zinc-800 bg-zinc-950/70">
            <div className="flex items-center justify-between border-b border-zinc-800 px-4 py-2.5">
                <span className="text-sm font-medium text-zinc-200">Live conversation</span>
                <span className={`flex items-center gap-1 text-xs ${connected ? "text-emerald-300" : "text-zinc-500"}`}>
                    {connected ? <Wifi className="h-3.5 w-3.5" /> : <WifiOff className="h-3.5 w-3.5" />}
                    {connected ? "live" : "connecting…"}
                </span>
            </div>

            <div className="flex-1 min-h-0 space-y-2 overflow-auto p-3">
                {lines.length === 0 ? (
                    <div className="flex h-full items-center justify-center text-sm text-zinc-500">
                        Waiting for the investigation to stream in…
                    </div>
                ) : (
                    lines.map((line) => (
                        <div key={line.key} className={`rounded-xl border px-3 py-2 ${roleClass(line.role)}`}>
                            <div className="text-xs font-medium text-zinc-400">{line.title}</div>
                            <div className="mt-0.5 whitespace-pre-wrap text-sm text-zinc-100">{line.content}</div>
                        </div>
                    ))
                )}
            </div>

            {error && <div className="px-3 pb-1 text-xs text-rose-300">{error}</div>}

            <div className="flex items-center gap-2 border-t border-zinc-800 p-3">
                <input
                    value={text}
                    onChange={(e) => setText(e.target.value)}
                    onKeyDown={(e) => {
                        if (e.key === "Enter" && !e.shiftKey) {
                            e.preventDefault()
                            void send()
                        }
                    }}
                    placeholder="Steer the investigation or ask for a metric…"
                    className="flex-1 rounded-lg border border-zinc-800 bg-zinc-900/70 px-3 py-2 text-sm text-zinc-100 placeholder:text-zinc-500 focus:border-cyan-500/50 focus:outline-none"
                />
                <Button
                    onClick={() => void send()}
                    disabled={sending || !text.trim()}
                    className="gap-2 bg-cyan-500 text-slate-950 hover:bg-cyan-400"
                >
                    {sending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
                    Send
                </Button>
            </div>
        </div>
    )
}
