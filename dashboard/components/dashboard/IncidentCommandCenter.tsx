"use client"

import { useEffect, useRef, useState, type ReactNode } from "react"
import { Card, CardContent } from "@/components/ui/card"
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
        agent_results?: Record<string, unknown>
        agents_invoked?: string[]
        thought_traces?: Record<string, string[]>
        [key: string]: unknown
    }
    error?: string
}

interface DebugSnapshot {
    status: string
    next: unknown[]
    values: NonNullable<IncidentStatusResponse["values"]>
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

function formatAgentLabel(agentName: string) {
    return agentName
        .replace(/_/g, " ")
        .split(" ")
        .filter(Boolean)
        .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
        .join(" ")
}

function thoughtToneClass(agentName: string) {
    if (agentName.toLowerCase() === "supervisor") {
        return "border-amber-500/20 bg-amber-500/8"
    }

    return "border-cyan-500/20 bg-cyan-500/8"
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

function normalizeActivityEntry(entry: LogEntry): string | null {
    const rawContent = (entry.result || entry.tool_args || "").trim()
    if (!rawContent) return null

    return stripBracketedTimestamp(rawContent).trim() || null
}

function agentToneClass(agentName: string) {
    switch (agentName.toLowerCase()) {
        case "supervisor":
            return "border-amber-500/20 bg-amber-500/8"
        case "kubernetes_agent":
            return "border-sky-500/20 bg-sky-500/8"
        case "metrics_agent":
            return "border-cyan-500/20 bg-cyan-500/8"
        case "logs_agent":
            return "border-emerald-500/20 bg-emerald-500/8"
        case "github_agent":
            return "border-zinc-500/20 bg-zinc-900/70"
        case "runbooks_agent":
            return "border-teal-500/20 bg-teal-500/8"
        case "reflector":
            return "border-stone-500/20 bg-stone-500/8"
        case "planner":
            return "border-orange-500/20 bg-orange-500/8"
        default:
            return thoughtToneClass(agentName)
    }
}

function summarizeAgentResult(value: unknown) {
    if (value == null) return ""

    if (typeof value === "string") {
        const trimmed = value.trim()
        if (!trimmed) return ""

        if ((trimmed.startsWith("{") && trimmed.endsWith("}")) || (trimmed.startsWith("[") && trimmed.endsWith("]"))) {
            try {
                return summarizeAgentResult(JSON.parse(trimmed))
            } catch {
                return trimmed
            }
        }

        return trimmed
    }

    if (Array.isArray(value)) {
        return value.map((item) => summarizeAgentResult(item)).filter(Boolean).join("\n")
    }

    if (typeof value === "object") {
        const record = value as Record<string, unknown>
        for (const key of ["summary", "message", "content", "result", "final_response", "hypothesis", "analysis"]) {
            const candidate = record[key]
            if (typeof candidate === "string" && candidate.trim()) {
                return candidate.trim()
            }
        }

        const stringEntries = Object.entries(record).filter(([, candidate]) => typeof candidate === "string" && candidate.trim())
        if (stringEntries.length > 0) {
            return stringEntries
                .slice(0, 3)
                .map(([key, candidate]) => `${formatAgentLabel(key)}: ${(candidate as string).trim()}`)
                .join("\n")
        }

        try {
            return JSON.stringify(record, null, 2)
        } catch {
            return String(value)
        }
    }

    return String(value).trim()
}

function sortChronologically(entries: ChatEntry[]) {
    return [...entries].sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime())
}

function buildAgentResultEntries(
    values?: IncidentStatusResponse["values"],
    baseTimestamp?: string,
): ChatEntry[] {
    const agentResults = values?.agent_results
    if (!agentResults || Object.keys(agentResults).length === 0) return []

    const baseTime = baseTimestamp ? new Date(baseTimestamp).getTime() : Date.now()
    const startTime = Number.isNaN(baseTime) ? Date.now() : baseTime
    const orderedAgents = values?.agents_invoked?.filter((agentName) => Boolean(agentResults[agentName])) || []
    const agentNames = orderedAgents.length > 0
        ? orderedAgents
        : Object.keys(agentResults)

    const entries: ChatEntry[] = []
    let index = 0

    for (const agentName of agentNames) {
        const content = summarizeAgentResult(agentResults[agentName])
        if (!content) continue

        entries.push({
            id: `agent-${agentName}-${index}`,
            role: "assistant",
            timestamp: new Date(startTime + index * 1000).toISOString(),
            title: formatAgentLabel(agentName),
            content,
            accent: agentToneClass(agentName),
            kind: "message",
        })
        index += 1
    }

    return entries
}

function buildTranscriptEntries(values?: IncidentStatusResponse["values"], baseTimestamp?: string) {
    const agentEntries = buildAgentResultEntries(values, baseTimestamp)
    if (agentEntries.length > 0) {
        return agentEntries
    }

    return buildThoughtTraceEntries(values, baseTimestamp)
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
    const orderedAgents = [
        "supervisor",
        "kubernetes_agent",
        "metrics_agent",
        "logs_agent",
        "github_agent",
        "runbooks_agent",
        "reflector",
        "planner",
        "aggregate",
    ]

    const agentNames = [
        ...orderedAgents.filter((agentName) => thoughtTraces[agentName]),
        ...Object.keys(thoughtTraces).filter((agentName) => !orderedAgents.includes(agentName)),
    ]

    for (const agentName of agentNames) {
        const traces = thoughtTraces[agentName] || []
        for (const trace of traces) {
            entries.push({
                id: `thought-${agentName}-${index}`,
                role: "assistant",
                timestamp: new Date(startTime + index * 1000).toISOString(),
                title: `${formatAgentLabel(agentName)} thinking`,
                content: trace,
                accent: thoughtToneClass(agentName),
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
    ]
}

function renderConversationContent(entry: ChatEntry) {
    if (entry.kind === "summary") {
        return <div className="space-y-3 text-sm leading-6 text-zinc-200">{renderMarkdownContent(entry.content)}</div>
    }

    if (entry.role === "assistant") {
        return <div className="space-y-3 text-sm leading-6 text-zinc-100">{renderMarkdownContent(entry.content)}</div>
    }

    return <p className="whitespace-pre-wrap text-sm leading-6 text-zinc-50">{entry.content}</p>
}

export function IncidentCommandCenter({ incident, refreshNonce }: IncidentCommandCenterProps) {
    const [loading, setLoading] = useState(false)
    const [sending, setSending] = useState(false)
    const [error, setError] = useState<string | null>(null)
    const [runtimeStatus, setRuntimeStatus] = useState<string>("UNKNOWN")
    const [summary, setSummary] = useState<string | null>(null)
    const [entries, setEntries] = useState<ChatEntry[]>([])
    const [activityFeed, setActivityFeed] = useState<string[]>([])
    const [draft, setDraft] = useState("")
    const [pendingTurn, setPendingTurn] = useState(false)
    const [graphActive, setGraphActive] = useState(false)
    const [debugSnapshot, setDebugSnapshot] = useState<DebugSnapshot | null>(null)
    const endRef = useRef<HTMLDivElement | null>(null)
    const transcriptSignatureRef = useRef<string>("")
    const hasSummary = Boolean(summary)
    const shouldPoll = (Boolean(incident) && !hasSummary) || pendingTurn || graphActive
    const chatStateLabel = hasSummary ? "Remediation ready" : formatStatusLabel(incident?.status || runtimeStatus)
    const chatStateClass = hasSummary
        ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-200"
        : statusToneClass(incident?.status || runtimeStatus)
    const runtimePillLabel = hasSummary ? "Summary ready" : graphActive ? "LangGraph live" : "Awaiting input"
    const runtimePillClass = hasSummary
        ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-200"
        : statusToneClass(runtimeStatus)

    const refreshConversation = async (selectedIncident: Incident) => {
        const [logsResult, statusResult] = await Promise.all([
            api.get(`/incidents/${selectedIncident.id}/logs`).catch((fetchError) => ({ error: fetchError })),
            api.get(`/incidents/${selectedIncident.id}/status`).catch((fetchError) => ({ error: fetchError })),
        ])

        const rawLogEntries = "data" in logsResult
            ? (logsResult.data as LogEntry[])
            : []

        const logEntries = rawLogEntries
            ? (logsResult.data as LogEntry[])
                .map(normalizeLogEntry)
                .filter((entry): entry is ChatEntry => Boolean(entry))
            : []
        const activityLines = rawLogEntries
            .map(normalizeActivityEntry)
            .filter((entry): entry is string => Boolean(entry))
            .slice(0, 12)

        const statusData = "data" in statusResult ? (statusResult.data as IncidentStatusResponse) : null
        const nextStatus = statusData?.status || selectedIncident.status.toUpperCase()
        const nextSummary = statusData?.values?.final_response || selectedIncident.summary || null
        const graphIsActive = Array.isArray(statusData?.next) && statusData.next.length > 0
        const statusValues = statusData?.values || null
        setActivityFeed(activityLines)
        const transcriptSignature = JSON.stringify({
            logs: logEntries.map((entry) => ({
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
        setRuntimeStatus(nextStatus)
        setSummary(nextSummary)
        setGraphActive(graphIsActive)
        setDebugSnapshot(statusValues ? {
            status: nextStatus,
            next: statusData?.next || [],
            values: statusValues,
        } : null)
        setPendingTurn((currentPendingTurn) => {
            if (graphIsActive) return true
            if (nextSummary) return false
            return currentPendingTurn
        })
        const nextEntries = sortChronologically(logEntries)
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
        setEntries(sortChronologically(nextEntries))

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

    const debugAgents = (() => {
        if (!debugSnapshot) return []

        const invoked = debugSnapshot.values.agents_invoked || []
        const agentResults = debugSnapshot.values.agent_results || {}
        return Array.from(new Set([
            ...invoked,
            ...Object.keys(agentResults),
            ...(debugSnapshot.values.thought_traces ? Object.keys(debugSnapshot.values.thought_traces) : []),
        ]))
    })()

    const routingReasoning = (() => {
        if (!debugSnapshot) return null

        const metadata = debugSnapshot.values.metadata as Record<string, unknown> | undefined
        const reasoning = metadata?.routing_reasoning
        return typeof reasoning === "string" ? reasoning : null
    })()

    const planText = (() => {
        if (!debugSnapshot) return null

        const metadata = debugSnapshot.values.metadata as Record<string, unknown> | undefined
        const plan = metadata?.plan_text
        return typeof plan === "string" ? plan : null
    })()

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
        <Card className="flex h-full min-h-0 overflow-hidden border-zinc-800 bg-[#0b0f17] text-zinc-100 shadow-2xl shadow-black/30">
            <CardContent className="grid min-h-0 flex-1 gap-0 p-0 lg:grid-cols-[minmax(0,1fr)_380px]">
                <section className="flex min-h-0 flex-col border-r border-zinc-800/70">
                    <ScrollArea className="min-h-0 flex-1 bg-[radial-gradient(circle_at_top,_rgba(34,211,238,0.05),_transparent_28%),linear-gradient(180deg,_rgba(2,6,23,0.2)_0%,_transparent_100%)] px-4 py-5 md:px-6">
                        <div className="mx-auto flex w-full max-w-3xl flex-col gap-5 pr-1">
                            {error && (
                                <div className="rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-200">
                                    {error}
                                </div>
                            )}

                            {!summary && loading && (
                                <div className="flex items-center gap-3 rounded-3xl border border-zinc-800/80 bg-zinc-950/70 p-4 text-sm text-zinc-400">
                                    <Loader2 className="h-4 w-4 animate-spin text-cyan-400" />
                                    Gathering evidence from the incident feed...
                                </div>
                            )}

                            {!summary && !loading && entries.length === 0 && (
                                <div className="rounded-3xl border border-dashed border-zinc-800/80 bg-zinc-950/50 px-6 py-14 text-center text-sm text-zinc-400">
                                    The assistant is waiting for a follow-up. Ask a question to queue another LangGraph turn.
                                </div>
                            )}

                            {entries.length === 0 ? (
                                <div className="rounded-[28px] border border-dashed border-zinc-800/80 bg-zinc-950/40 px-6 py-20 text-center text-sm text-zinc-500">
                                    <div className="mx-auto mb-3 flex h-11 w-11 items-center justify-center rounded-full border border-zinc-800/80 bg-zinc-950/80 text-cyan-400">
                                        <Sparkles className="h-4 w-4" />
                                    </div>
                                    No transcript yet. The next assistant reply will appear here automatically.
                                </div>
                            ) : (
                                entries.map((entry) => {
                                    const isUser = entry.role === "user"
                                    const isSummary = entry.kind === "summary"
                                    return (
                                        <div key={entry.id} className={`flex items-end gap-3 ${isUser ? "justify-end" : "justify-start"}`}>
                                            {!isUser && (
                                                <div className="mb-1 flex h-9 w-9 shrink-0 items-center justify-center rounded-full border border-cyan-500/20 bg-cyan-500/10 text-cyan-300">
                                                    <Bot className="h-4 w-4" />
                                                </div>
                                            )}
                                            <div className={`flex max-w-[82%] flex-col gap-1 ${isUser ? "items-end" : "items-start"}`}>
                                                <div
                                                    className={`rounded-3xl px-4 py-3 shadow-lg shadow-black/10 ${isUser ? "border border-cyan-500/25 bg-cyan-500 text-slate-950" : isSummary ? "border border-emerald-500/20 bg-emerald-500/10" : "border border-zinc-800/80 bg-zinc-950/80"}`}
                                                >
                                                    <div className="mb-2 flex items-center justify-between gap-3 text-[11px] uppercase tracking-[0.24em] text-zinc-500">
                                                        <span className="flex items-center gap-2">
                                                            {isUser ? (
                                                                <User className="h-3.5 w-3.5" />
                                                            ) : (
                                                                <span className="text-zinc-300">{isSummary ? "Summary" : "SRE Agent"}</span>
                                                            )}
                                                            {isUser ? "You" : isSummary ? "Summary" : "Assistant"}
                                                        </span>
                                                        <span>{formatTimeLabel(entry.timestamp)}</span>
                                                    </div>
                                                    {renderConversationContent(entry)}
                                                </div>
                                            </div>
                                            {isUser && (
                                                <div className="mb-1 flex h-9 w-9 shrink-0 items-center justify-center rounded-full border border-cyan-500/30 bg-cyan-500 text-slate-950">
                                                    <User className="h-4 w-4" />
                                                </div>
                                            )}
                                        </div>
                                    )
                                })
                            )}

                            {pendingTurn && !hasSummary && (
                                <div className="flex items-start gap-3 justify-start">
                                    <div className="mb-1 flex h-9 w-9 shrink-0 items-center justify-center rounded-full border border-cyan-500/20 bg-cyan-500/10 text-cyan-300">
                                        <Bot className="h-4 w-4" />
                                    </div>
                                    <div className="max-w-[82%] rounded-3xl border border-zinc-800/80 bg-zinc-950/80 px-4 py-3 shadow-lg shadow-black/10">
                                        <div className="mb-2 flex items-center justify-between gap-3 text-[11px] uppercase tracking-[0.24em] text-zinc-500">
                                            <span className="flex items-center gap-2">
                                                <span className="text-zinc-300">Assistant</span>
                                                Thinking
                                            </span>
                                            <span>now</span>
                                        </div>
                                        <p className="text-sm leading-6 text-zinc-400">
                                            The agent is working on the follow-up and will reply here when the turn completes.
                                        </p>
                                    </div>
                                </div>
                            )}
                            <div ref={endRef} />
                        </div>
                    </ScrollArea>

                    <div className="border-t border-zinc-800/70 bg-zinc-950/75 px-4 py-4 md:px-6">
                        <div className="mx-auto max-w-3xl rounded-[28px] border border-zinc-800/80 bg-[#0d1320] p-4 shadow-inner shadow-black/25">
                            <div className="mb-3 flex items-center justify-between gap-3 text-xs text-zinc-500">
                                <span>Ask about metrics, logs, rollout risk, or code changes.</span>
                                <span className="rounded-full border border-zinc-800 bg-zinc-900/80 px-2.5 py-1 font-mono text-[10px] uppercase tracking-[0.24em] text-zinc-400">
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
                                    className="min-h-[92px] w-full resize-none rounded-[22px] border border-zinc-800/80 bg-zinc-950 px-4 py-3 text-sm text-zinc-50 outline-none transition placeholder:text-zinc-600 focus:border-cyan-500/40 focus:ring-2 focus:ring-cyan-500/10"
                                />
                                <Button
                                    onClick={() => void handleSend()}
                                    disabled={sending || !draft.trim()}
                                    className="h-full min-h-[92px] rounded-[22px] bg-cyan-500 px-6 text-slate-950 hover:bg-cyan-400"
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
                </section>

                <aside className="flex min-h-0 flex-col gap-4 overflow-y-auto bg-zinc-950/55 p-4 md:p-6">
                    <div className="rounded-[28px] border border-zinc-800/80 bg-zinc-950/70 p-4 shadow-inner shadow-black/20">
                        <div className="flex items-start justify-between gap-3">
                            <div className="min-w-0 space-y-2">
                                <p className="text-[11px] uppercase tracking-[0.24em] text-zinc-500">Incident context</p>
                                <h2 className="text-xl font-semibold tracking-tight text-white">{incident.title}</h2>
                                <p className="text-sm leading-6 text-zinc-400">
                                    {incident.description || "No additional incident description is available."}
                                </p>
                            </div>
                        </div>

                        <div className="mt-4 flex flex-wrap gap-2">
                            <Badge variant="outline" className={severityToneClass(incident.severity)}>
                                {incident.severity.toUpperCase()}
                            </Badge>
                            <Badge variant="outline" className={chatStateClass}>
                                {chatStateLabel}
                            </Badge>
                            <div className={`rounded-full border px-3 py-1 text-[11px] uppercase tracking-[0.24em] ${runtimePillClass}`}>
                                {runtimePillLabel}
                            </div>
                        </div>

                        <div className="mt-4 grid gap-2 text-sm text-zinc-300">
                            <div className="rounded-2xl border border-zinc-800 bg-zinc-950/70 px-3 py-2">
                                <span className="text-zinc-500">Opened</span> {formatTimeLabel(incident.created_at)}
                            </div>
                            {incident.resolved_at && (
                                <div className="rounded-2xl border border-zinc-800 bg-zinc-950/70 px-3 py-2">
                                    <span className="text-zinc-500">Resolved</span> {formatTimeLabel(incident.resolved_at)}
                                </div>
                            )}
                            <div className="rounded-2xl border border-zinc-800 bg-zinc-950/70 px-3 py-2">
                                <span className="text-zinc-500">Mode</span> Human-in-the-loop
                            </div>
                        </div>
                    </div>

                    <div className="rounded-[28px] border border-zinc-800/80 bg-zinc-950/70 p-4 shadow-inner shadow-black/20">
                        <p className="text-[11px] uppercase tracking-[0.24em] text-zinc-500">Recent activity</p>
                        <div className="mt-3 space-y-2">
                            {activityFeed.length > 0 ? activityFeed.map((line, index) => (
                                <div key={`${index}-${line}`} className="rounded-2xl border border-zinc-800 bg-zinc-950/80 px-3 py-2 text-xs leading-5 text-zinc-300">
                                    {line}
                                </div>
                            )) : (
                                <p className="text-sm text-zinc-500">No activity captured yet.</p>
                            )}
                        </div>
                    </div>

                    <details className="group rounded-[28px] border border-zinc-800/80 bg-zinc-950/70 p-4">
                        <summary className="cursor-pointer list-none text-xs uppercase tracking-[0.24em] text-zinc-400">
                            Debug context
                        </summary>
                        <div className="mt-4 grid gap-3">
                            <div className="rounded-2xl border border-zinc-800 bg-zinc-950/80 p-4">
                                <p className="text-[11px] uppercase tracking-[0.24em] text-zinc-500">LangGraph state</p>
                                <div className="mt-3 space-y-2 text-sm text-zinc-200">
                                    <p><span className="text-zinc-500">status:</span> {debugSnapshot?.status || runtimeStatus}</p>
                                    <p><span className="text-zinc-500">next:</span> {debugSnapshot?.next?.length ? JSON.stringify(debugSnapshot.next) : "[]"}</p>
                                    <p><span className="text-zinc-500">graph active:</span> {graphActive ? "true" : "false"}</p>
                                    <p><span className="text-zinc-500">pending turn:</span> {pendingTurn ? "true" : "false"}</p>
                                    <p><span className="text-zinc-500">summary ready:</span> {hasSummary ? "true" : "false"}</p>
                                </div>
                            </div>

                            <div className="rounded-2xl border border-zinc-800 bg-zinc-950/80 p-4">
                                <p className="text-[11px] uppercase tracking-[0.24em] text-zinc-500">Supervisor context</p>
                                <div className="mt-3 space-y-2 text-sm text-zinc-200">
                                    <p><span className="text-zinc-500">routing reasoning:</span> {routingReasoning || "-"}</p>
                                    <p><span className="text-zinc-500">plan text:</span> {planText ? "available" : "-"}</p>
                                    <p><span className="text-zinc-500">ooda phase:</span> {typeof debugSnapshot?.values.ooda_phase === "string" ? debugSnapshot.values.ooda_phase : "-"}</p>
                                    <p><span className="text-zinc-500">final response:</span> {typeof debugSnapshot?.values.final_response === "string" && debugSnapshot.values.final_response ? "available" : "-"}</p>
                                </div>
                            </div>

                            <div className="rounded-2xl border border-zinc-800 bg-zinc-950/80 p-4">
                                <p className="text-[11px] uppercase tracking-[0.24em] text-zinc-500">Agents involved</p>
                                <div className="mt-3 flex flex-wrap gap-2">
                                    {debugAgents.length > 0 ? debugAgents.map((agentName) => (
                                        <span key={agentName} className="rounded-full border border-cyan-500/20 bg-cyan-500/10 px-3 py-1 text-xs text-cyan-200">
                                            {agentName}
                                        </span>
                                    )) : (
                                        <span className="text-sm text-zinc-500">No agent results yet</span>
                                    )}
                                </div>
                            </div>

                            <div className="rounded-2xl border border-zinc-800 bg-zinc-950/80 p-4">
                                <p className="text-[11px] uppercase tracking-[0.24em] text-zinc-500">Raw graph state</p>
                                <pre className="mt-3 max-h-72 overflow-auto rounded-2xl border border-zinc-800 bg-zinc-950 px-4 py-3 text-xs leading-5 text-zinc-300">
{JSON.stringify(debugSnapshot?.values || {}, null, 2)}
                                </pre>
                            </div>
                        </div>
                    </details>
                </aside>
            </CardContent>
        </Card>
    )
}
