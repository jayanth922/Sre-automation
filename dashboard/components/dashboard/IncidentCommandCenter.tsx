"use client"

import { useEffect, useRef, useState } from "react"
import { Bot, Loader2, Send, Sparkles } from "lucide-react"

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

interface TranscriptEvent {
    id: string
    incident_id: string
    sequence: number
    event_type: string
    speaker_role: string
    title: string | null
    content: string
    payload: Record<string, unknown> | null
    created_at: string
}

interface IncidentTranscriptResponse {
    incident: Incident
    conversation_mode: "investigation" | "assistant"
    summary: string | null
    events: TranscriptEvent[]
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
    role: "user" | "assistant" | "system"
    timestamp: string
    sequence?: number | null
    title: string
    content: string
    accent: string
    kind?: "message" | "summary" | "system"
    speakerRole: string
    eventType: string
    payload?: Record<string, unknown> | null
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

function speakerLabel(role: string) {
    switch (role) {
        case "user":
            return "You"
        case "supervisor":
            return "Supervisor"
        case "prometheus_specialist":
            return "Prometheus Specialist"
        case "loki_specialist":
            return "Loki Specialist"
        case "github_specialist":
            return "GitHub Specialist"
        case "runbooks_specialist":
            return "Runbooks Specialist"
        case "system":
            return "System"
        default:
            return role.replace(/_/g, " ").replace(/\b\w/g, (character) => character.toUpperCase())
    }
}

// Removed accentClassForSpeaker for WhatsApp redesign

function mapTranscriptEvent(event: TranscriptEvent): ChatEntry {
    const role = event.speaker_role || "system"
    const kind = event.event_type === "summary" ? "summary" : role === "system" ? "system" : "message"

    return {
        id: event.id,
        role: role === "user" ? "user" : role === "system" ? "system" : "assistant",
        timestamp: event.created_at,
        sequence: event.sequence,
        title: event.title || speakerLabel(role),
        content: event.content,
        accent: "",
        kind,
        speakerRole: role,
        eventType: event.event_type,
        payload: event.payload,
    }
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
        sequence: null,
        title: isUser ? "You" : "SRE Agent",
        content,
        accent: "",
        kind: "message",
        speakerRole: isUser ? "user" : "assistant",
        eventType: isUser ? "human_message" : "assistant_message",
        payload: null,
    }
}

function sortChronologically(entries: ChatEntry[]) {
    return [...entries].sort((a, b) => {
        const leftSequence = a.sequence ?? null
        const rightSequence = b.sequence ?? null

        if (leftSequence !== null && rightSequence !== null && leftSequence !== rightSequence) {
            return leftSequence - rightSequence
        }

        const leftTime = new Date(a.timestamp).getTime()
        const rightTime = new Date(b.timestamp).getTime()
        if (leftTime !== rightTime) {
            return leftTime - rightTime
        }

        return a.id.localeCompare(b.id)
    })
}

type ParticipantTone = {
    titleClass: string
    bodyClass: string
    metaClass: string
    avatarClass: string
    bubbleClass: string
}

function getParticipantTone(entry: ChatEntry): ParticipantTone {
    if (entry.role === "user") {
        return {
            titleClass: "text-cyan-200",
            bodyClass: "text-sky-50",
            metaClass: "text-cyan-100/70",
            avatarClass: "bg-cyan-400/15 text-cyan-100",
            bubbleClass: "border-cyan-400/15 bg-cyan-500/10",
        }
    }

    switch (entry.speakerRole) {
        case "supervisor":
            return {
                titleClass: "text-emerald-200",
                bodyClass: "text-emerald-50",
                metaClass: "text-emerald-100/70",
                avatarClass: "bg-emerald-400/15 text-emerald-100",
                bubbleClass: "border-emerald-400/15 bg-emerald-500/10",
            }
        case "prometheus_specialist":
            return {
                titleClass: "text-sky-200",
                bodyClass: "text-sky-50",
                metaClass: "text-sky-100/70",
                avatarClass: "bg-sky-400/15 text-sky-100",
                bubbleClass: "border-sky-400/15 bg-sky-500/10",
            }
        case "loki_specialist":
            return {
                titleClass: "text-orange-200",
                bodyClass: "text-orange-50",
                metaClass: "text-orange-100/70",
                avatarClass: "bg-orange-400/15 text-orange-100",
                bubbleClass: "border-orange-400/15 bg-orange-500/10",
            }
        case "github_specialist":
            return {
                titleClass: "text-fuchsia-200",
                bodyClass: "text-fuchsia-50",
                metaClass: "text-fuchsia-100/70",
                avatarClass: "bg-fuchsia-400/15 text-fuchsia-100",
                bubbleClass: "border-fuchsia-400/15 bg-fuchsia-500/10",
            }
        case "runbooks_specialist":
            return {
                titleClass: "text-lime-200",
                bodyClass: "text-lime-50",
                metaClass: "text-lime-100/70",
                avatarClass: "bg-lime-400/15 text-lime-100",
                bubbleClass: "border-lime-400/15 bg-lime-500/10",
            }
        default:
            return {
                titleClass: "text-slate-200",
                bodyClass: "text-slate-50",
                metaClass: "text-slate-400",
                avatarClass: "bg-slate-500/15 text-slate-100",
                bubbleClass: "border-white/5 bg-slate-900/75",
            }
    }
}

function TranscriptEntryCard({ entry, index }: { entry: ChatEntry; index: number }) {
    const isUser = entry.role === "user"
    const displayTitle = entry.kind === "summary" ? "Supervisor" : entry.title
    const tone = getParticipantTone(entry)
    const [mounted, setMounted] = useState(false)

    useEffect(() => {
        const frame = requestAnimationFrame(() => setMounted(true))
        return () => cancelAnimationFrame(frame)
    }, [])

    return (
        <div
            className={`flex items-end gap-3 transition-all duration-300 ease-out ${isUser ? "justify-end" : "justify-start"} ${mounted ? "translate-y-0 opacity-100" : "translate-y-2 opacity-0"}`}
            style={{ transitionDelay: `${Math.min(index * 55, 240)}ms` }}
        >
            {!isUser && (
                <div className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-sm font-semibold ${tone.avatarClass}`}>
                    {displayTitle.charAt(0)}
                </div>
            )}

            <div
                className={`w-full max-w-[88%] sm:max-w-[72%] rounded-[24px] border px-4 py-3 shadow-sm transition-shadow duration-200 ${tone.bubbleClass} ${isUser ? "ml-auto" : ""}`}
            >
                <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0 flex-1">
                        <div className={`text-[12px] font-medium ${tone.titleClass}`}>
                            {isUser ? "You" : displayTitle}
                        </div>
                        <div className={`mt-1 whitespace-pre-wrap text-[14px] leading-6 break-words ${tone.bodyClass}`}>
                            {entry.content}
                        </div>
                    </div>
                    <div className={`shrink-0 text-[11px] ${tone.metaClass}`}>
                        {formatTimeLabel(entry.timestamp)}
                    </div>
                </div>
            </div>
        </div>
    )
}

export function IncidentCommandCenter({ incident, refreshNonce }: IncidentCommandCenterProps) {
    const [loading, setLoading] = useState(false)
    const [sending, setSending] = useState(false)
    const [error, setError] = useState<string | null>(null)
    const [summary, setSummary] = useState<string | null>(null)
    const [conversationMode, setConversationMode] = useState<"investigation" | "assistant">("investigation")
    const [entries, setEntries] = useState<ChatEntry[]>([])
    const [draft, setDraft] = useState("")
    const [pendingTurn, setPendingTurn] = useState(false)
    const [graphActive, setGraphActive] = useState(false)
    const endRef = useRef<HTMLDivElement | null>(null)
    const scrollAreaRef = useRef<HTMLDivElement | null>(null)
    const shouldStickToBottomRef = useRef(true)
    const transcriptSignatureRef = useRef<string>("")
    const hasSummary = Boolean(summary)
    const shouldPoll = Boolean(incident) && (pendingTurn || graphActive || conversationMode === "assistant" || !hasSummary)

    const refreshConversation = async (selectedIncident: Incident) => {
        const [transcriptResult, logsResult, statusResult] = await Promise.all([
            api.get(`/incidents/${selectedIncident.id}/transcript`).catch((fetchError: unknown) => ({ error: fetchError })),
            api.get(`/incidents/${selectedIncident.id}/logs`).catch((fetchError: unknown) => ({ error: fetchError })),
            api.get(`/incidents/${selectedIncident.id}/status`).catch((fetchError: unknown) => ({ error: fetchError })),
        ])

        const transcriptData = "data" in transcriptResult ? (transcriptResult.data as IncidentTranscriptResponse) : null
        const canonicalEntries = transcriptData?.events.map(mapTranscriptEvent) || []
        const fallbackLogEntries = canonicalEntries.length > 0
            ? []
            : ("data" in logsResult ? (logsResult.data as LogEntry[]).map(normalizeLogEntry).filter((entry): entry is ChatEntry => Boolean(entry)) : [])

        const statusData = "data" in statusResult ? (statusResult.data as IncidentStatusResponse) : null
        const nextStatus = statusData?.status || selectedIncident.status.toUpperCase()
        const nextSummary = transcriptData?.summary || statusData?.values?.final_response || selectedIncident.summary || null
        const nextConversationMode = transcriptData?.conversation_mode || (nextSummary ? "assistant" : "investigation")
        const graphIsActive = Array.isArray(statusData?.next) && statusData.next.length > 0

        const transcriptEntries: ChatEntry[] = []
        if (nextSummary && !canonicalEntries.some((entry) => entry.eventType === "summary")) {
            transcriptEntries.push({
                id: `summary-${selectedIncident.id}`,
                role: "assistant",
                timestamp: selectedIncident.resolved_at || new Date().toISOString(),
                sequence: null,
                title: "Supervisor",
                content: nextSummary,
                accent: "border-cyan-500/20 bg-cyan-500/8",
                kind: "summary",
                speakerRole: "supervisor",
                eventType: "summary",
            })
        }

        const primaryEntries = canonicalEntries.length > 0 ? canonicalEntries : fallbackLogEntries
        const combinedEntries = sortChronologically([...primaryEntries, ...transcriptEntries])
        const transcriptSignature = JSON.stringify({
            transcript: combinedEntries.map((entry) => ({
                id: entry.id,
                title: entry.title,
                content: entry.content,
                role: entry.role,
                sequence: entry.sequence,
                speakerRole: entry.speakerRole,
                eventType: entry.eventType,
            })),
            status: nextStatus,
            summary: nextSummary,
            conversationMode: nextConversationMode,
        })

        if (transcriptSignature === transcriptSignatureRef.current) {
            setError(null)
            return
        }

        transcriptSignatureRef.current = transcriptSignature
        setSummary(nextSummary)
        setConversationMode(nextConversationMode)
        setGraphActive(graphIsActive)
        setPendingTurn((currentPendingTurn: boolean) => {
            if (graphIsActive) return true
            if (nextSummary) return false
            return currentPendingTurn
        })
        setEntries(combinedEntries)

        if (!("data" in transcriptResult) && !("data" in logsResult)) {
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
                sortChronologically([
                    ...current,
                    {
                        id: `draft-${Date.now()}`,
                        role: "user",
                        timestamp: new Date().toISOString(),
                        sequence: null,
                        title: "You",
                        content: message,
                        accent: "border-cyan-500/30 bg-cyan-500/10",
                        kind: "message",
                        speakerRole: "user",
                        eventType: "human_message",
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
            setConversationMode("investigation")
            setError(null)
            setPendingTurn(false)
            setGraphActive(false)
            shouldStickToBottomRef.current = true
            transcriptSignatureRef.current = ""
            return
        }

        shouldStickToBottomRef.current = true

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
        if (!shouldStickToBottomRef.current) return
        endRef.current?.scrollIntoView({ behavior: entries.length > 1 ? "smooth" : "auto", block: "end" })
    }, [entries.length, loading])

    const handleScroll = () => {
        const scrollElement = scrollAreaRef.current
        if (!scrollElement) return

        const distanceFromBottom = scrollElement.scrollHeight - scrollElement.scrollTop - scrollElement.clientHeight
        shouldStickToBottomRef.current = distanceFromBottom < 72
    }

    if (!incident) {
        return (
            <Card className="flex min-h-[680px] overflow-hidden border border-slate-800 bg-slate-950 text-slate-100 shadow-sm">
                <CardContent className="flex flex-1 items-center justify-center p-8 text-center text-sm text-slate-400">
                    Select an incident to open the chat thread.
                </CardContent>
            </Card>
        )
    }

    return (
        <Card className="flex h-full min-h-0 overflow-hidden border border-slate-800 bg-slate-950 text-slate-100 shadow-sm">
            <CardContent className="flex min-h-0 flex-1 flex-col p-0">
                <ScrollArea ref={scrollAreaRef} onScroll={handleScroll} className="min-h-0 flex-1 bg-slate-950 px-4 py-5 md:px-6">
                    <div className="flex w-full flex-col gap-4 pr-1">

                        {error && (
                            <div className="rounded-2xl border border-rose-500/20 bg-rose-500/10 px-4 py-3 text-sm text-rose-100">
                                {error}
                            </div>
                        )}

                        {!summary && loading && (
                            <div className="flex items-center gap-3 rounded-2xl border border-slate-800 bg-slate-900/70 p-4 text-sm text-slate-300 shadow-sm">
                                <Loader2 className="h-4 w-4 animate-spin text-cyan-400" />
                                Gathering evidence and agent turns from the incident feed...
                            </div>
                        )}

                        {!summary && !loading && entries.length === 0 && (
                            <div className="rounded-2xl border border-slate-800 bg-slate-900/70 px-6 py-14 text-center text-sm text-slate-400 shadow-sm">
                                The board is waiting for a follow-up. Interrupt the thread to queue another turn.
                            </div>
                        )}

                        {entries.length === 0 ? (
                            <div className="rounded-2xl border border-slate-800 bg-slate-900/70 px-6 py-20 text-center text-sm text-slate-400 shadow-sm">
                                <div className="mx-auto mb-3 flex h-11 w-11 items-center justify-center rounded-full bg-slate-800 text-cyan-300">
                                    <Sparkles className="h-4 w-4" />
                                </div>
                                No transcript yet. The next speaker turn will appear here automatically.
                            </div>
                        ) : (
                            entries.map((entry: ChatEntry, index: number) => (
                                <TranscriptEntryCard key={entry.id} entry={entry} index={index} />
                            ))
                        )}

                        {pendingTurn && (conversationMode === "assistant" || !hasSummary) && (
                            <div className="flex items-start justify-start gap-3">
                                <div className="mb-1 flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-slate-800 text-cyan-300">
                                    <Bot className="h-4 w-4" />
                                </div>
                                <div className="max-w-[82%] rounded-2xl border border-slate-800 bg-slate-900/70 px-4 py-3 shadow-sm">
                                    <div className="mb-2 flex items-center justify-between gap-3 text-[11px] text-slate-400">
                                        <span>Assistant thinking</span>
                                        <span>now</span>
                                    </div>
                                    <p className="text-sm leading-6 text-slate-300">
                                        The team is lining up the next response and will answer here when the turn completes.
                                    </p>
                                </div>
                            </div>
                        )}
                        <div ref={endRef} />
                    </div>
                </ScrollArea>

                <div className="border-t border-slate-800 px-4 py-4 md:px-6">
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
                            placeholder={summary ? `Ask a follow-up about ${incident.title.toLowerCase()}...` : `Ask the agent about ${incident.title.toLowerCase()}...`}
                            className="min-h-[92px] flex-1 resize-none rounded-2xl border border-slate-800 bg-slate-900/80 px-4 py-3 text-sm text-slate-100 outline-none transition placeholder:text-slate-500 focus:border-cyan-500/30 focus:ring-2 focus:ring-cyan-500/10"
                        />
                        <Button
                            onClick={() => void handleSend()}
                            disabled={sending || !draft.trim()}
                            className="mb-2 mr-2 flex h-[50px] w-12 shrink-0 items-center justify-center self-end rounded-full bg-slate-100 p-0 text-slate-950 shadow-sm transition-transform duration-200 hover:-translate-y-0.5 hover:bg-white"
                        >
                            {sending ? <Loader2 className="h-5 w-5 animate-spin" /> : <Send className="h-5 w-5 ml-1" />}
                        </Button>
                    </div>
                </div>
            </CardContent>
        </Card>
    )
}
