"use client"

import { useEffect, useRef, useState, type ReactNode } from "react"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Bot, Loader2, Send, Sparkles, User } from "lucide-react"
import { api } from "@/lib/auth-context"

interface Incident {
    id: string
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
        thought_traces?: Record<string, string[]>
        [key: string]: unknown
    }
    error?: string
}

interface IncidentCommandCenterProps {
    incident: Incident | null
    refreshNonce: number
}

interface ChatEntry {
    id: string
    role: "user" | "assistant"
    timestamp: string
    title: string
    content: string
    accent: string
    kind?: "message" | "thought" | "summary"
}

function stripBracketedTimestamp(value: string) {
    return value
        .split("\n")
        .map((line) => line.replace(/^\[(?:\s*\d{1,2}:\d{2}(?::\d{2})?|\s*\d{4}-\d{2}-\d{2}[^\]]*)\]\s*/, ""))
        .join("\n")
}

function normalizeLogEntry(entry: LogEntry): ChatEntry | null {
    const rawContent = (entry.result || entry.tool_args || "").trim()
    if (!rawContent) return null

    const userMatch = rawContent.match(/^USER:\s*(.*)$/i)
    const assistantMatch = rawContent.match(/^ASSISTANT:\s*(.*)$/i)
    const content = stripBracketedTimestamp((userMatch?.[1] || assistantMatch?.[1] || rawContent).trim())
    const isUser = Boolean(userMatch)

    return {
        id: entry.id,
        role: isUser ? "user" : "assistant",
        timestamp: entry.timestamp || new Date().toISOString(),
        title: isUser ? "You" : entry.agent_name || "Agent",
        content,
        accent: isUser ? "border-cyan-500/30 bg-cyan-500/10" : "border-zinc-800 bg-zinc-950/90",
        kind: "message",
    }
}

function formatTimeLabel(timestamp: string) {
    const date = new Date(timestamp)
    if (Number.isNaN(date.getTime())) return "now"
    return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
}

function formatStatusLabel(status: string) {
    switch (status.toLowerCase()) {
        case "open":
            return "Queued"
        case "investigating":
            return "Investigating"
        case "running":
            return "Investigating"
        case "waiting_approval":
            return "Processing"
        case "resolved":
            return "Resolved"
        default:
            return status
    }
}

function severityToneClass(severity: string) {
    switch (severity.toLowerCase()) {
        case "critical":
            return "border-rose-500/30 bg-rose-500/10 text-rose-200"
        case "high":
            return "border-orange-500/30 bg-orange-500/10 text-orange-200"
        case "medium":
            return "border-amber-500/30 bg-amber-500/10 text-amber-200"
        default:
            return "border-sky-500/30 bg-sky-500/10 text-sky-200"
    }
}

function statusToneClass(status: string) {
    switch (status.toLowerCase()) {
        case "open":
        case "queued":
            return "border-amber-500/30 bg-amber-500/10 text-amber-200"
        case "resolved":
            return "border-emerald-500/30 bg-emerald-500/10 text-emerald-200"
        case "running":
        case "investigating":
        case "waiting_approval":
            return "border-cyan-500/30 bg-cyan-500/10 text-cyan-200"
        default:
            return "border-zinc-700 bg-zinc-900/80 text-zinc-300"
    }
}

function renderInlineText(value: string): ReactNode[] {
    const segments: ReactNode[] = []
    const parts = value.split(/(`[^`]+`|\*\*[^*]+\*\*)/g)

    parts.forEach((part, index) => {
        if (!part) return

        if (part.startsWith("`") && part.endsWith("`")) {
            segments.push(
                <code key={`code-${index}`} className="rounded bg-zinc-900 px-1.5 py-0.5 font-mono text-[0.92em] text-cyan-200">
                    {part.slice(1, -1)}
                </code>,
            )
            return
        }

        if (part.startsWith("**") && part.endsWith("**")) {
            segments.push(
                <strong key={`strong-${index}`} className="font-semibold text-zinc-50">
                    {part.slice(2, -2)}
                </strong>,
            )
            return
        }

        segments.push(part)
    })

    return segments
}

function renderMarkdownContent(markdown: string): ReactNode[] {
    const lines = markdown.replace(/\r/g, "").split("\n")
    const blocks: ReactNode[] = []
    let index = 0

    while (index < lines.length) {
        const rawLine = lines[index]
        const line = rawLine.trimEnd()

        if (!line.trim()) {
            index += 1
            continue
        }

        if (line.startsWith("```")) {
            const codeLines: string[] = []
            index += 1

            while (index < lines.length && !lines[index].trim().startsWith("```")) {
                codeLines.push(lines[index])
                index += 1
            }

            blocks.push(
                <pre key={`code-block-${index}`} className="overflow-x-auto rounded-2xl border border-zinc-800 bg-zinc-950 px-4 py-3 text-sm text-zinc-200">
                    <code className="font-mono leading-6">{codeLines.join("\n")}</code>
                </pre>,
            )

            while (index < lines.length && !lines[index].trim().startsWith("```")) {
                index += 1
            }
            index += 1
            continue
        }

        if (/^#{1,3}\s+/.test(line)) {
            const headingText = line.replace(/^#{1,3}\s+/, "")
            blocks.push(
                <p key={`heading-${index}`} className="text-sm font-semibold uppercase tracking-[0.24em] text-cyan-200/80">
                    {headingText}
                </p>,
            )
            index += 1
            continue
        }

        if (/^[-*]\s+/.test(line)) {
            const items: string[] = []
            while (index < lines.length && /^[-*]\s+/.test(lines[index].trim())) {
                items.push(lines[index].trim().replace(/^[-*]\s+/, ""))
                index += 1
            }

            blocks.push(
                <ul key={`list-${index}`} className="space-y-2 text-sm leading-6 text-zinc-200">
                    {items.map((item, itemIndex) => (
                        <li key={`${index}-${itemIndex}`} className="flex gap-3">
                            <span className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-cyan-300" />
                            <span>{renderInlineText(item)}</span>
                        </li>
                    ))}
                </ul>,
            )
            continue
        }

        if (/^\d+\.\s+/.test(line)) {
            const items: string[] = []
            while (index < lines.length && /^\d+\.\s+/.test(lines[index].trim())) {
                items.push(lines[index].trim().replace(/^\d+\.\s+/, ""))
                index += 1
            }

            blocks.push(
                <ol key={`ordered-${index}`} className="space-y-2 text-sm leading-6 text-zinc-200">
                    {items.map((item, itemIndex) => (
                        <li key={`${index}-${itemIndex}`} className="flex gap-3">
                            <span className="min-w-5 font-mono text-xs text-zinc-500">{itemIndex + 1}.</span>
                            <span>{renderInlineText(item)}</span>
                        </li>
                    ))}
                </ol>,
            )
            continue
        }

        const paragraphLines = [line.trim()]
        index += 1
        while (index < lines.length && lines[index].trim() && !/^([#>*-]|\d+\.)\s+/.test(lines[index].trim())) {
            paragraphLines.push(lines[index].trim())
            index += 1
        }

        blocks.push(
            <p key={`paragraph-${index}`} className="whitespace-pre-wrap text-sm leading-6 text-zinc-200">
                {renderInlineText(paragraphLines.join(" "))}
            </p>,
        )
    }

    return blocks
}

function buildThoughtTraceEntries(
    values?: IncidentStatusResponse["values"],
    baseTimestamp?: string,
): ChatEntry[] {
    const thoughtTraces = values?.thought_traces
    if (!thoughtTraces) return []

    const entries: ChatEntry[] = []
    let index = 0
    const baseTime = baseTimestamp ? new Date(baseTimestamp).getTime() : Date.now()
    const startTime = Number.isNaN(baseTime) ? Date.now() : baseTime

    for (const [agentName, traces] of Object.entries(thoughtTraces)) {
        for (const trace of traces) {
            entries.push({
                id: `thought-${agentName}-${index}`,
                role: "assistant",
                timestamp: new Date(startTime + index * 1000).toISOString(),
                title: agentName.toLowerCase() === "supervisor" ? "Supervisor trace" : `${agentName} thought`,
                content: trace,
                accent: "border-cyan-500/20 bg-cyan-500/8",
                kind: "thought",
            })
            index += 1
        }
    }

    return entries
}

function mergeEntries(primary: ChatEntry[], secondary: ChatEntry[]) {
    const seen = new Set(primary.map((entry) => `${entry.title}::${entry.content}`))
    return [
        ...primary,
        ...secondary.filter((entry) => {
            const key = `${entry.title}::${entry.content}`
            if (seen.has(key)) return false
            seen.add(key)
            return true
        }),
    ].sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime())
}

export function IncidentCommandCenter({ incident, refreshNonce }: IncidentCommandCenterProps) {
    const [loading, setLoading] = useState(false)
    const [sending, setSending] = useState(false)
    const [error, setError] = useState<string | null>(null)
    const [runtimeStatus, setRuntimeStatus] = useState<string>("UNKNOWN")
    const [summary, setSummary] = useState<string | null>(null)
    const [entries, setEntries] = useState<ChatEntry[]>([])
    const [draft, setDraft] = useState("")
    const [pendingTurn, setPendingTurn] = useState(false)
    const endRef = useRef<HTMLDivElement | null>(null)
    const transcriptSignatureRef = useRef<string>("")
    const activeExecution = ["running", "investigating", "waiting_approval"].includes(runtimeStatus.toLowerCase())
    const shouldPoll = activeExecution || pendingTurn
    const hasSummary = Boolean(summary)
    const chatStateLabel = hasSummary ? "Remediation ready" : formatStatusLabel(incident?.status || runtimeStatus)
    const chatStateClass = hasSummary
        ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-200"
        : statusToneClass(incident?.status || runtimeStatus)
    const runtimePillLabel = hasSummary ? "Summary ready" : activeExecution ? "LangGraph live" : "Awaiting input"
    const runtimePillClass = hasSummary
        ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-200"
        : statusToneClass(runtimeStatus)

    const refreshConversation = async (selectedIncident: Incident) => {
        const [logsResult, statusResult] = await Promise.all([
            api.get(`/incidents/${selectedIncident.id}/logs`).catch((fetchError) => ({ error: fetchError })),
            api.get(`/incidents/${selectedIncident.id}/status`).catch((fetchError) => ({ error: fetchError })),
        ])

        const logEntries = "data" in logsResult
            ? (logsResult.data as LogEntry[])
                .map(normalizeLogEntry)
                .filter((entry): entry is ChatEntry => Boolean(entry))
            : []

        const statusData = "data" in statusResult ? (statusResult.data as IncidentStatusResponse) : null
        const nextStatus = statusData?.status || selectedIncident.status.toUpperCase()
        const nextSummary = statusData?.values?.final_response || selectedIncident.summary || null
        const thoughtEntries = buildThoughtTraceEntries(statusData?.values, selectedIncident.created_at)
        const transcriptSignature = JSON.stringify({
            logs: logEntries.map((entry) => ({
                id: entry.id,
                title: entry.title,
                content: entry.content,
                role: entry.role,
            })),
            thoughts: thoughtEntries.map((entry) => ({
                id: entry.id,
                title: entry.title,
                content: entry.content,
                role: entry.role,
            })),
            status: nextStatus,
            summary: nextSummary,
        })

        setRuntimeStatus(nextStatus)
        setSummary(nextSummary)
        setPendingTurn((currentPendingTurn) => {
            const nextIsActive = ["running", "investigating", "waiting_approval"].includes(nextStatus.toLowerCase())
            if (nextIsActive) return true
            if (nextSummary && transcriptSignature !== transcriptSignatureRef.current) return false
            return currentPendingTurn
        })

        if (transcriptSignature === transcriptSignatureRef.current) {
            setError(null)
            return
        }

        transcriptSignatureRef.current = transcriptSignature
        const nextEntries = mergeEntries(logEntries, thoughtEntries)
        if (nextSummary) {
            nextEntries.push({
                id: `summary-${selectedIncident.id}`,
                role: "assistant",
                timestamp: selectedIncident.resolved_at || new Date().toISOString(),
                title: "Remediation summary",
                content: nextSummary,
                accent: "border-cyan-500/20 bg-cyan-500/8",
                kind: "summary",
            })
        }
        setEntries(nextEntries)

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

        try {
            await api.post(`/incidents/${incident.id}/message`, { message })
            setDraft("")
            setEntries((current: ChatEntry[]) =>
                mergeEntries(current, [
                    {
                        id: `draft-${Date.now()}`,
                        role: "user",
                        timestamp: new Date().toISOString(),
                        title: "You",
                        content: message,
                        accent: "border-cyan-500/30 bg-cyan-500/10",
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
            setRuntimeStatus("UNKNOWN")
            setSummary(null)
            setError(null)
            setPendingTurn(false)
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
            <Card className="flex min-h-[680px] overflow-hidden border-zinc-800 bg-zinc-950/80 text-zinc-100 shadow-2xl shadow-black/30">
                <CardContent className="flex flex-1 items-center justify-center p-8 text-center text-sm text-zinc-500">
                    Select an incident to open the chat thread.
                </CardContent>
            </Card>
        )
    }

    return (
        <Card className="flex h-full min-h-0 overflow-hidden border-zinc-800 bg-zinc-950/80 text-zinc-100 shadow-2xl shadow-black/30">
            <CardHeader className="border-b border-zinc-800 bg-zinc-950/70">
                <div className="flex flex-wrap items-start justify-between gap-4">
                    <div className="space-y-2">
                        <div className="flex flex-wrap items-center gap-3">
                            <CardTitle className="flex items-center gap-3 text-base font-semibold tracking-tight text-white">
                                <Sparkles className="h-4 w-4 text-cyan-400" />
                                Investigation Chat
                            </CardTitle>
                            <Badge variant="outline" className={severityToneClass(incident.severity)}>
                                {incident.severity.toUpperCase()}
                            </Badge>
                            <Badge
                                variant="outline"
                                className={chatStateClass}
                            >
                                {chatStateLabel}
                            </Badge>
                        </div>
                        <CardDescription className="max-w-2xl text-sm text-zinc-400">
                            One continuous investigation thread for {incident.title}. Keep follow-up questions here and read the reasoning stream in the same place.
                        </CardDescription>
                    </div>

                    <div className={`rounded-full border px-3 py-1 text-[11px] uppercase tracking-[0.24em] ${runtimePillClass}`}>
                        {runtimePillLabel}
                    </div>
                </div>
                <div className="mt-4 flex flex-wrap items-center gap-2 text-[11px] uppercase tracking-[0.24em] text-zinc-500">
                    <span className="rounded-full border border-zinc-800 bg-zinc-950/80 px-3 py-1">Opened {formatTimeLabel(incident.created_at)}</span>
                    {incident.resolved_at && (
                        <span className="rounded-full border border-zinc-800 bg-zinc-950/80 px-3 py-1">Resolved {formatTimeLabel(incident.resolved_at)}</span>
                    )}
                    <span className="rounded-full border border-zinc-800 bg-zinc-950/80 px-3 py-1">Human-in-the-loop</span>
                </div>
            </CardHeader>

            <CardContent className="flex min-h-0 flex-1 flex-col gap-0 overflow-hidden p-0">
                <ScrollArea className="min-h-0 flex-1 px-5 py-4">
                    <div className="space-y-4 pr-2">
                        {error && (
                            <div className="rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-200">
                                {error}
                            </div>
                        )}

                        {!summary && loading && (
                            <div className="flex items-center gap-3 rounded-2xl border border-zinc-800 bg-zinc-950/60 p-4 text-sm text-zinc-400">
                                <Loader2 className="h-4 w-4 animate-spin text-cyan-400" />
                                Gathering evidence from the incident feed...
                            </div>
                        )}

                        {!summary && !loading && entries.length === 0 && (
                            <div className="rounded-2xl border border-zinc-800 bg-zinc-950/60 p-4 text-sm text-zinc-400">
                                The assistant is waiting for a follow-up. Ask a question to queue another LangGraph turn.
                            </div>
                        )}

                        {entries.length === 0 ? (
                            <div className="rounded-3xl border border-dashed border-zinc-800 bg-zinc-950/40 px-6 py-14 text-center text-sm text-zinc-500">
                                <div className="mx-auto mb-3 flex h-10 w-10 items-center justify-center rounded-full border border-zinc-800 bg-zinc-950/80 text-cyan-400">
                                    <Sparkles className="h-4 w-4" />
                                </div>
                                No transcript yet. The next agent turn will appear here automatically.
                            </div>
                        ) : (
                            entries.map((entry) => {
                                const isUser = entry.role === "user"
                                const isSummary = entry.kind === "summary"
                                const isThought = entry.kind === "thought"
                                return (
                                    <div key={entry.id} className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
                                        <div
                                            className={`max-w-[88%] rounded-3xl px-4 py-3 shadow-lg shadow-black/10 ${isUser ? "border border-cyan-500/20 bg-cyan-500/10" : isSummary ? "bg-cyan-500/8" : isThought ? "bg-cyan-500/6" : "bg-zinc-950/55"}`}
                                        >
                                            <div className="mb-2 flex items-center justify-between gap-3 text-[11px] uppercase tracking-[0.24em] text-zinc-500">
                                                <span className="flex items-center gap-2">
                                                    {isUser ? (
                                                        <User className="h-3.5 w-3.5 text-cyan-300" />
                                                    ) : (
                                                        <Bot className="h-3.5 w-3.5 text-cyan-300" />
                                                    )}
                                                    {isUser ? "You" : entry.title}
                                                </span>
                                                <span>{formatTimeLabel(entry.timestamp)}</span>
                                            </div>
                                            {isSummary ? (
                                                <div className="space-y-3 text-sm leading-6 text-zinc-200">
                                                    {renderMarkdownContent(entry.content)}
                                                </div>
                                            ) : (
                                                <p className="whitespace-pre-wrap text-sm leading-6 text-zinc-100">
                                                    {entry.content}
                                                </p>
                                            )}
                                        </div>
                                    </div>
                                )
                            })
                        )}
                        <div ref={endRef} />
                    </div>
                </ScrollArea>

                <div className="border-t border-zinc-800 px-5 py-4">
                    <div className="rounded-3xl border border-zinc-800 bg-zinc-950/90 p-4 shadow-inner shadow-black/20">
                        <div className="mb-3 flex items-center justify-between gap-3 text-xs text-zinc-500">
                            <span>Ask about metrics, logs, rollout risk, or code changes.</span>
                            <span className="rounded-full border border-zinc-800 bg-zinc-900 px-2.5 py-1 font-mono text-[10px] uppercase tracking-[0.24em] text-zinc-400">
                                Chat mode
                            </span>
                        </div>
                        <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_auto]">
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
                                className="min-h-[108px] w-full resize-none rounded-2xl border border-zinc-800 bg-zinc-950 px-4 py-3 text-sm text-zinc-100 outline-none transition placeholder:text-zinc-600 focus:border-cyan-500/40 focus:ring-2 focus:ring-cyan-500/10"
                            />
                            <Button
                                onClick={() => void handleSend()}
                                disabled={sending || !draft.trim()}
                                className="h-full min-h-[108px] rounded-2xl bg-cyan-500 px-6 text-slate-950 hover:bg-cyan-400"
                            >
                                {sending ? (
                                    <Loader2 className="h-4 w-4 animate-spin" />
                                ) : (
                                    <Send className="h-4 w-4" />
                                )}
                                Send
                            </Button>
                        </div>
                    </div>
                </div>
            </CardContent>
        </Card>
    )
}
