"use client"

import { useEffect, useRef, useState } from "react"
import { Bot, Loader2, Send, Sparkles, User } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { ScrollArea } from "@/components/ui/scroll-area"
import { api } from "@/lib/auth-context"

interface Incident {
    id: string
    cluster_id: string
    title: string
    description: string | null
    severity: string
    status: string
    summary: string | null
    created_at: string
    resolved_at: string | null
}

interface LogEntry {
    id: string
    timestamp: string | null
    agent_name: string
    tool_name: string
    tool_args: string
    status: string
    result: string | null
    error_message: string | null
}

interface IncidentStatusResponse {
    status?: string
    next?: unknown[]
    values?: {
        final_response?: string | null
        [key: string]: unknown
    }
    error?: string
}

interface ChatEntry {
    id: string
    role: "user" | "assistant"
    timestamp: string
    title: string
    content: string
    accent: string
    kind?: "message" | "summary"
}

interface IncidentCommandCenterProps {
    incident: Incident | null
    refreshNonce: number
}

function stripBracketedTimestamp(value: string) {
    return value
        .split("\n")
        .map((line) => line.replace(/^\[(?:\s*\d{1,2}:\d{2}(?::\d{2})?|\s*\d{4}-\d{2}-\d{2}[^\]]*)\]\s*/, ""))
        .join("\n")
}

function formatTimeLabel(timestamp: string) {
    const date = new Date(timestamp)
    if (Number.isNaN(date.getTime())) return "now"
    return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
}

function normalizeLogEntry(entry: LogEntry): ChatEntry | null {
    const rawContent = (entry.result || entry.tool_args || "").trim()
    if (!rawContent) return null

    const userMatch = rawContent.match(/^USER:\s*(.*)$/i)
    const assistantMatch = rawContent.match(/^ASSISTANT:\s*(.*)$/i)
    if (!userMatch && !assistantMatch) return null

    const content = stripBracketedTimestamp((userMatch?.[1] || assistantMatch?.[1] || rawContent).trim())
    if (!content) return null

    const isUser = Boolean(userMatch)

    return {
        id: entry.id,
        role: isUser ? "user" : "assistant",
        timestamp: entry.timestamp || new Date().toISOString(),
        title: isUser ? "You" : "SRE Agent",
        content,
        accent: isUser ? "border-cyan-500/30 bg-cyan-500/10" : "border-zinc-800 bg-zinc-950/90",
        kind: "message",
    }
}

function sortChronologically(entries: ChatEntry[]) {
    return [...entries].sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime())
}

function renderMessageContent(content: string) {
    return <p className="whitespace-pre-wrap text-sm leading-6 text-zinc-100">{content}</p>
}

function renderTranscriptEntry(entry: ChatEntry) {
    const isUser = entry.role === "user"
    const isSummary = entry.kind === "summary"

    return (
        <div key={entry.id} className={`flex items-start gap-3 ${isUser ? "justify-end" : "justify-start"}`}>
            {!isUser && (
                <div className={`mb-1 flex h-10 w-10 shrink-0 items-center justify-center rounded-full border ${isSummary ? "border-emerald-500/25 bg-emerald-500/10 text-emerald-200" : "border-cyan-500/20 bg-cyan-500/10 text-cyan-300"}`}>
                    {isSummary ? <Sparkles className="h-4 w-4" /> : <Bot className="h-4 w-4" />}
                </div>
            )}

            <article className={`max-w-[86%] rounded-[30px] border px-4 py-4 shadow-lg shadow-black/10 ${entry.accent} ${isUser ? "text-cyan-50" : ""}`}>
                <div className="mb-3 flex items-center justify-between gap-3 text-[11px] uppercase tracking-[0.24em] text-zinc-500">
                    <div className="flex min-w-0 flex-wrap items-center gap-2">
                        <span className={`rounded-full border px-2.5 py-1 font-medium ${isUser ? "border-cyan-500/30 bg-cyan-500/15 text-cyan-100" : isSummary ? "border-emerald-500/25 bg-emerald-500/10 text-emerald-200" : "border-zinc-800 bg-zinc-950/90 text-zinc-300"}`}>
                            {isUser ? "Human interruption" : isSummary ? "Supervisor" : entry.title}
                        </span>
                        <span className="truncate rounded-full border border-zinc-800 bg-zinc-950/70 px-2.5 py-1 text-zinc-400">
                            {isUser ? "guiding the investigation" : isSummary ? "final summary" : "agent report"}
                        </span>
                    </div>
                    <span>{formatTimeLabel(entry.timestamp)}</span>
                </div>
                {renderMessageContent(entry.content)}
            </article>

            {isUser && (
                <div className="mb-1 flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-cyan-500/30 bg-cyan-500 text-slate-950">
                    <User className="h-4 w-4" />
                </div>
            )}
        </div>
    )
}

export function IncidentCommandCenter({ incident, refreshNonce }: IncidentCommandCenterProps) {
    const [loading, setLoading] = useState(false)
    const [sending, setSending] = useState(false)
    const [error, setError] = useState<string | null>(null)
    const [summary, setSummary] = useState<string | null>(null)
    const [entries, setEntries] = useState<ChatEntry[]>([])
    const [draft, setDraft] = useState("")
    const [pendingTurn, setPendingTurn] = useState(false)
    const [graphActive, setGraphActive] = useState(false)
    const endRef = useRef<HTMLDivElement | null>(null)
    const transcriptSignatureRef = useRef<string>("")
    const hasSummary = Boolean(summary)
    const shouldPoll = (Boolean(incident) && !hasSummary) || pendingTurn || graphActive

    const refreshConversation = async (selectedIncident: Incident) => {
        const [logsResult, statusResult] = await Promise.all([
            api.get(`/incidents/${selectedIncident.id}/logs`).catch((fetchError) => ({ error: fetchError })),
            api.get(`/incidents/${selectedIncident.id}/status`).catch((fetchError) => ({ error: fetchError })),
        ])

        const rawLogEntries = "data" in logsResult ? (logsResult.data as LogEntry[]) : []
        const logEntries = rawLogEntries.map(normalizeLogEntry).filter((entry): entry is ChatEntry => Boolean(entry))

        const statusData = "data" in statusResult ? (statusResult.data as IncidentStatusResponse) : null
        const nextStatus = statusData?.status || selectedIncident.status.toUpperCase()
        const nextSummary = statusData?.values?.final_response || selectedIncident.summary || null
        const graphIsActive = Array.isArray(statusData?.next) && statusData.next.length > 0

        const transcriptEntries: ChatEntry[] = []
        if (nextSummary) {
            transcriptEntries.push({
                id: `summary-${selectedIncident.id}`,
                role: "assistant",
                timestamp: selectedIncident.resolved_at || new Date().toISOString(),
                title: "Supervisor",
                content: nextSummary,
                accent: "border-cyan-500/20 bg-cyan-500/8",
                kind: "summary",
            })
        }

        const combinedEntries = sortChronologically([...logEntries, ...transcriptEntries])
        const transcriptSignature = JSON.stringify({
            transcript: combinedEntries.map((entry) => ({
                id: entry.id,
                title: entry.title,
                content: entry.content,
                role: entry.role,
            })),
            status: nextStatus,
            summary: nextSummary,
        })

        if (transcriptSignature === transcriptSignatureRef.current) {
            setError(null)
            return
        }

        transcriptSignatureRef.current = transcriptSignature
        setSummary(nextSummary)
        setGraphActive(graphIsActive)
        setPendingTurn((currentPendingTurn) => {
            if (graphIsActive) return true
            if (nextSummary) return false
            return currentPendingTurn
        })
        setEntries(combinedEntries)

        if (!("data" in logsResult)) {
            setError("Transcript temporarily unavailable. The agent status is still loaded.")
        } else {
            setError(null)
        }
    }

    const handleSend = async () => {
        if (!incident) return

        const message = draft.trim()
        if (!message || sending) return

        setSending(true)
        setError(null)
        setPendingTurn(true)
        setSummary(null)

        try {
            await api.post(`/incidents/${incident.id}/message`, { message })
            setDraft("")
            setEntries((current) =>
                sortChronologically([
                    ...current,
                    {
                        id: `draft-${Date.now()}`,
                        role: "user",
                        timestamp: new Date().toISOString(),
                        title: "You",
                        content: message,
                        accent: "border-cyan-500/30 bg-cyan-500/10",
                        kind: "message",
                    },
                ]),
            )
            await refreshConversation(incident)
        } catch (sendError: unknown) {
            const errorMessage = sendError instanceof Error ? sendError.message : "Failed to send message"
            setError(errorMessage)
        } finally {
            setSending(false)
        }
    }

    useEffect(() => {
        const selectedId = incident?.id
        if (!selectedId) {
            setEntries([])
            setSummary(null)
            setError(null)
            setPendingTurn(false)
            setGraphActive(false)
            transcriptSignatureRef.current = ""
            return
        }

        let active = true
        let intervalId: ReturnType<typeof setInterval> | undefined

        const fetchConversation = async () => {
            try {
                if (!active) return
                await refreshConversation(incident)
            } catch (fetchError: unknown) {
                if (!active) return
                const errorMessage = fetchError instanceof Error ? fetchError.message : "Failed to load incident conversation"
                setError(errorMessage)
            } finally {
                if (active) {
                    setLoading(false)
                }
            }
        }

        setLoading(true)
        void fetchConversation()

        if (shouldPoll) {
            intervalId = setInterval(() => {
                void fetchConversation()
            }, 15000)
        }

        return () => {
            active = false
            if (intervalId) {
                clearInterval(intervalId)
            }
        }
    }, [incident, incident?.id, incident?.status, incident?.created_at, incident?.summary, incident?.title, refreshNonce, shouldPoll])

    useEffect(() => {
        endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" })
    }, [entries, loading])

    if (!incident) {
        return (
            <Card className="flex min-h-[680px] overflow-hidden border-0 bg-zinc-950/80 text-zinc-100 shadow-none">
                <CardContent className="flex flex-1 items-center justify-center p-8 text-center text-sm text-zinc-500">
                    Select an incident to open the chat thread.
                </CardContent>
            </Card>
        )
    }

    return (
        <Card className="flex h-full min-h-0 overflow-hidden border-0 bg-[#0b101a] text-zinc-100 shadow-none">
            <CardContent className="flex min-h-0 flex-1 flex-col p-0">
                <ScrollArea className="min-h-0 flex-1 px-4 py-5 md:px-6">
                    <div className="flex w-full flex-col gap-5 pr-1">
                        {error && (
                            <div className="rounded-2xl bg-rose-500/10 px-4 py-3 text-sm text-rose-200">
                                {error}
                            </div>
                        )}

                        {!summary && loading && (
                            <div className="flex items-center gap-3 rounded-3xl bg-zinc-950/70 p-4 text-sm text-zinc-400">
                                <Loader2 className="h-4 w-4 animate-spin text-cyan-400" />
                                Gathering evidence and agent turns from the incident feed...
                            </div>
                        )}

                        {!summary && !loading && entries.length === 0 && (
                            <div className="rounded-3xl bg-zinc-950/50 px-6 py-14 text-center text-sm text-zinc-400">
                                The board is waiting for a follow-up. Interrupt the thread to queue another turn.
                            </div>
                        )}

                        {entries.length === 0 ? (
                            <div className="rounded-[28px] bg-zinc-950/40 px-6 py-20 text-center text-sm text-zinc-500">
                                <div className="mx-auto mb-3 flex h-11 w-11 items-center justify-center rounded-full bg-zinc-950/80 text-cyan-400">
                                    <Sparkles className="h-4 w-4" />
                                </div>
                                No transcript yet. The next speaker turn will appear here automatically.
                            </div>
                        ) : (
                            entries.map((entry) => renderTranscriptEntry(entry))
                        )}

                        {pendingTurn && !hasSummary && (
                            <div className="flex items-start justify-start gap-3">
                                <div className="mb-1 flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-cyan-500/10 text-cyan-300">
                                    <Bot className="h-4 w-4" />
                                </div>
                                <div className="max-w-[82%] rounded-[30px] bg-zinc-950/80 px-4 py-3 shadow-lg shadow-black/10">
                                    <div className="mb-2 flex items-center justify-between gap-3 text-[11px] uppercase tracking-[0.24em] text-zinc-500">
                                        <span className="flex items-center gap-2">
                                            <span className="text-zinc-300">Board</span>
                                            Thinking
                                        </span>
                                        <span>now</span>
                                    </div>
                                    <p className="text-sm leading-6 text-zinc-400">
                                        The team is lining up the next response and will answer here when the turn completes.
                                    </p>
                                </div>
                            </div>
                        )}
                        <div ref={endRef} />
                    </div>
                </ScrollArea>

                <div className="px-4 py-4 md:px-6">
                    <div className="flex w-full gap-3">
                        <textarea
                            value={draft}
                            onChange={(event) => setDraft(event.target.value)}
                            onKeyDown={(event) => {
                                if (event.key === "Enter" && !event.shiftKey) {
                                    event.preventDefault()
                                    void handleSend()
                                }
                            }}
                            placeholder={`Ask the agent about ${incident.title.toLowerCase()}...`}
                            className="min-h-[92px] flex-1 resize-none rounded-[22px] bg-zinc-950 px-4 py-3 text-sm text-zinc-50 outline-none transition placeholder:text-zinc-600 focus:ring-2 focus:ring-cyan-500/10"
                        />
                        <Button
                            onClick={() => void handleSend()}
                            disabled={sending || !draft.trim()}
                            className="min-h-[92px] rounded-[22px] bg-cyan-500 px-6 text-slate-950 hover:bg-cyan-400"
                        >
                            {sending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
                            Send
                        </Button>
                    </div>
                </div>
            </CardContent>
        </Card>
    )
}
