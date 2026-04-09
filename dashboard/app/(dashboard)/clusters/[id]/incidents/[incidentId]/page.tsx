"use client"

import { useCallback, useEffect, useMemo, useState } from "react"
import { useParams, useRouter } from "next/navigation"
import { ArrowLeft, Loader2, RefreshCw } from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { IncidentCommandCenter } from "@/components/dashboard/IncidentCommandCenter"
import { api } from "@/lib/auth-context"

interface Cluster {
    id: string
    name: string
    status: string
    last_heartbeat?: string | null
}

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

function severityClass(severity: string) {
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

function statusClass(status: string) {
    switch (status.toLowerCase()) {
        case "resolved":
            return "border-emerald-500/30 bg-emerald-500/10 text-emerald-200"
        case "investigating":
            return "border-cyan-500/30 bg-cyan-500/10 text-cyan-200"
        default:
            return "border-amber-500/30 bg-amber-500/10 text-amber-200"
    }
}

function formatTime(value?: string | null) {
    if (!value) return "-"
    const date = new Date(value)
    if (Number.isNaN(date.getTime())) return value
    return date.toLocaleString([], {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
    })
}

export default function IncidentDashboardPage() {
    const router = useRouter()
    const params = useParams()
    const clusterId = params.id as string
    const incidentId = params.incidentId as string

    const [cluster, setCluster] = useState<Cluster | null>(null)
    const [incident, setIncident] = useState<Incident | null>(null)
    const [loading, setLoading] = useState(true)
    const [refreshing, setRefreshing] = useState(false)
    const [refreshNonce, setRefreshNonce] = useState(0)
    const [error, setError] = useState<string | null>(null)

    const loadIncident = useCallback(async (initial = false) => {
        if (initial) {
            setLoading(true)
        }
        setRefreshing(true)
        setError(null)

        try {
            const [clustersResponse, incidentsResponse] = await Promise.all([
                api.get("/clusters"),
                api.get(`/clusters/${clusterId}/incidents`),
            ])

            const matchedCluster = (clustersResponse.data as Cluster[]).find((item) => item.id === clusterId) || null
            const clusterIncidents = incidentsResponse.data as Incident[]
            const matchedIncident = clusterIncidents.find((item) => item.id === incidentId) || clusterIncidents[0] || null

            setCluster(matchedCluster)
            setIncident(matchedIncident)
        } catch (loadError) {
            setError(loadError instanceof Error ? loadError.message : "Failed to load incident dashboard")
        } finally {
            setRefreshing(false)
            setLoading(false)
        }
    }, [clusterId, incidentId])

    useEffect(() => {
        void loadIncident(true)
    }, [loadIncident])

    useEffect(() => {
        const interval = setInterval(() => {
            void loadIncident()
        }, 12000)

        return () => clearInterval(interval)
    }, [loadIncident])

    const liveFlags = useMemo(() => {
        if (!incident) {
            return []
        }

        return [
            { label: "Status", value: incident.status },
            { label: "Severity", value: incident.severity },
            { label: "Open since", value: formatTime(incident.created_at) },
            { label: "Resolution", value: incident.resolved_at ? "Closed" : "Open" },
        ]
    }, [incident])

    if (loading) {
        return (
            <div className="flex min-h-[60vh] items-center justify-center text-zinc-400">
                <Loader2 className="h-10 w-10 animate-spin" />
            </div>
        )
    }

    if (!incident) {
        return (
            <Card className="border-zinc-800 bg-zinc-950/70 shadow-2xl shadow-black/20">
                <CardContent className="flex min-h-[320px] flex-col items-center justify-center gap-4 p-8 text-center text-zinc-400">
                    <p>Incident not found.</p>
                    <Button onClick={() => router.push(`/clusters/${clusterId}/incidents`)} className="gap-2 bg-cyan-500 text-slate-950 hover:bg-cyan-400">
                        Back to incidents
                    </Button>
                </CardContent>
            </Card>
        )
    }

    return (
        <div className="grid h-full min-h-0 w-full flex-1 gap-6 overflow-hidden xl:grid-cols-[minmax(320px,420px)_minmax(0,1fr)] xl:items-stretch">
            <aside className="xl:sticky xl:top-24">
                <Card className="h-full overflow-hidden border-0 bg-zinc-950/70 shadow-none backdrop-blur">
                    <CardContent className="space-y-5 p-5">
                        <div className="flex items-start justify-between gap-4">
                            <div className="flex min-w-0 items-start gap-4">
                                <Button variant="ghost" size="icon" onClick={() => router.push(`/clusters/${clusterId}/incidents`)} className="shrink-0 border border-zinc-800 bg-zinc-950/60 text-zinc-300 hover:bg-zinc-900 hover:text-white">
                                    <ArrowLeft className="h-5 w-5" />
                                </Button>
                                <div className="min-w-0 space-y-2">
                                    <div className="flex flex-wrap items-center gap-2">
                                        <Badge variant="outline" className={severityClass(incident.severity)}>
                                            {incident.severity}
                                        </Badge>
                                        <Badge variant="outline" className={statusClass(incident.status)}>
                                            {incident.status}
                                        </Badge>
                                    </div>
                                    <h1 className="text-2xl font-semibold tracking-tight text-white">{incident.title}</h1>
                                    <p className="text-sm leading-6 text-zinc-400">
                                        {incident.description || "This incident dashboard is focused on the current thread, live status signals, and the assistant chat surface."}
                                    </p>
                                </div>
                            </div>

                            <Button
                                variant="outline"
                                onClick={() => {
                                    setRefreshNonce((current) => current + 1)
                                    void loadIncident()
                                }}
                                disabled={refreshing}
                                className="gap-2 border-0 bg-zinc-950/60 text-zinc-200 hover:bg-zinc-900"
                            >
                                {refreshing ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
                                Refresh
                            </Button>
                        </div>

                        <div className="grid gap-3 sm:grid-cols-2">
                            {liveFlags.map((flag) => (
                                <div key={flag.label} className="rounded-2xl bg-zinc-900/50 px-4 py-3 text-sm text-zinc-300">
                                    <div className="text-[11px] uppercase tracking-[0.24em] text-zinc-500">{flag.label}</div>
                                    <div className="mt-1 text-zinc-100">{flag.value}</div>
                                </div>
                            ))}
                        </div>

                        <div className="grid gap-3 rounded-2xl bg-zinc-900/40 p-4 text-sm text-zinc-300 sm:grid-cols-2">
                            <div>
                                <div className="text-[11px] uppercase tracking-[0.24em] text-zinc-500">Incident ID</div>
                                <div className="mt-1 break-all font-mono text-xs text-zinc-400">{incident.id}</div>
                            </div>
                            <div>
                                <div className="text-[11px] uppercase tracking-[0.24em] text-zinc-500">Cluster</div>
                                <div className="mt-1 text-zinc-100">{cluster?.name || clusterId}</div>
                            </div>
                            <div>
                                <div className="text-[11px] uppercase tracking-[0.24em] text-zinc-500">Opened</div>
                                <div className="mt-1 text-zinc-100">{formatTime(incident.created_at)}</div>
                            </div>
                            <div>
                                <div className="text-[11px] uppercase tracking-[0.24em] text-zinc-500">Resolved</div>
                                <div className="mt-1 text-zinc-100">{incident.resolved_at ? formatTime(incident.resolved_at) : "Open"}</div>
                            </div>
                        </div>

                        {error && (
                            <div className="rounded-2xl bg-amber-500/10 px-4 py-3 text-sm text-amber-100">
                                {error}
                            </div>
                        )}
                    </CardContent>
                </Card>
            </aside>

            <main className="min-h-0">
                <IncidentCommandCenter incident={incident} refreshNonce={refreshNonce} />
            </main>
        </div>
    )
}