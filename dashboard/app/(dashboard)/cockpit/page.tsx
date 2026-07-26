"use client"

import { useCallback, useEffect, useMemo, useState } from "react"
import { useRouter } from "next/navigation"
import { Activity, CheckCircle2, Loader2, RefreshCw, ShieldCheck, TriangleAlert } from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { api } from "@/lib/auth-context"

// Multi-incident cockpit (project #4, Superset/T3-style): monitor every
// investigation running in parallel across clusters, and review/approve the
// plans that are waiting on a human — all from one surface.

interface Cluster {
    id: string
    name: string
    status: string
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

interface EnrichedIncident extends Incident {
    clusterName: string
    liveStatus?: string
    waitingApproval?: boolean
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

function formatTime(value?: string | null) {
    if (!value) return "-"
    const date = new Date(value)
    if (Number.isNaN(date.getTime())) return value
    return date.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })
}

export default function CockpitPage() {
    const router = useRouter()
    const [incidents, setIncidents] = useState<EnrichedIncident[]>([])
    const [loading, setLoading] = useState(true)
    const [refreshing, setRefreshing] = useState(false)
    const [error, setError] = useState<string | null>(null)
    const [approving, setApproving] = useState<string | null>(null)

    const loadAll = useCallback(async (initial = false) => {
        if (initial) setLoading(true)
        setRefreshing(true)
        setError(null)

        try {
            const clustersResponse = await api.get("/clusters")
            const clusters = clustersResponse.data as Cluster[]

            const perCluster = await Promise.all(
                clusters.map((cluster) =>
                    api
                        .get(`/clusters/${cluster.id}/incidents`)
                        .then((response) =>
                            (response.data as Incident[]).map((incident) => ({
                                ...incident,
                                clusterName: cluster.name,
                            }))
                        )
                        .catch(() => [] as EnrichedIncident[])
                )
            )

            let flat: EnrichedIncident[] = perCluster.flat()

            // Enrich non-resolved incidents with live LangGraph status (best-effort;
            // requires the checkpointer, so failures are tolerated).
            const active = flat.filter((incident) => incident.status.toLowerCase() !== "resolved")
            const statuses = await Promise.all(
                active.map(async (incident) => {
                    try {
                        const statusResponse = await api.get(`/incidents/${incident.id}/status`)
                        return { id: incident.id, live: statusResponse.data?.status as string | undefined }
                    } catch {
                        return { id: incident.id, live: undefined }
                    }
                })
            )
            const liveMap = new Map(statuses.map((entry) => [entry.id, entry.live]))

            flat = flat.map((incident) => ({
                ...incident,
                liveStatus: liveMap.get(incident.id),
                waitingApproval: liveMap.get(incident.id) === "WAITING_APPROVAL",
            }))

            setIncidents(flat)
        } catch (loadError) {
            setError(loadError instanceof Error ? loadError.message : "Failed to load cockpit")
        } finally {
            setRefreshing(false)
            setLoading(false)
        }
    }, [])

    const approve = async (incidentId: string) => {
        setApproving(incidentId)
        setError(null)
        try {
            await api.post(`/incidents/${incidentId}/approve`)
            await loadAll()
        } catch (approveError) {
            setError(approveError instanceof Error ? approveError.message : "Approve failed (admin only?)")
        } finally {
            setApproving(null)
        }
    }

    useEffect(() => {
        void loadAll(true)
    }, [loadAll])

    useEffect(() => {
        const interval = setInterval(() => void loadAll(), 10000)
        return () => clearInterval(interval)
    }, [loadAll])

    const awaiting = useMemo(() => incidents.filter((incident) => incident.waitingApproval), [incidents])
    const active = useMemo(
        () => incidents.filter((incident) => !incident.waitingApproval && incident.status.toLowerCase() !== "resolved"),
        [incidents]
    )
    const resolved = useMemo(
        () => incidents.filter((incident) => incident.status.toLowerCase() === "resolved"),
        [incidents]
    )

    const openIncident = (incident: EnrichedIncident) =>
        router.push(`/clusters/${incident.cluster_id}/incidents/${incident.id}`)

    return (
        <div className="flex w-full min-w-0 flex-1 flex-col gap-6">
            <header className="flex flex-col gap-4 rounded-3xl border border-zinc-800 bg-zinc-950/70 p-5 shadow-2xl shadow-black/20 backdrop-blur md:flex-row md:items-center md:justify-between">
                <div className="space-y-2">
                    <div className="flex flex-wrap items-center gap-3">
                        <h1 className="text-2xl font-semibold tracking-tight text-white md:text-3xl">Mission Control Cockpit</h1>
                        <Badge variant="outline" className="border-cyan-500/30 bg-cyan-500/10 text-cyan-200">
                            {active.length} active
                        </Badge>
                        {awaiting.length > 0 && (
                            <Badge variant="outline" className="border-amber-500/30 bg-amber-500/10 text-amber-200">
                                {awaiting.length} awaiting approval
                            </Badge>
                        )}
                    </div>
                    <p className="max-w-3xl text-sm text-zinc-400">
                        Every investigation running in parallel across your clusters, plus the remediation plans waiting on a human. Review and approve without leaving this surface.
                    </p>
                </div>
                <Button
                    variant="outline"
                    onClick={() => void loadAll()}
                    disabled={refreshing}
                    className="gap-2 border-zinc-800 bg-zinc-950/60 text-zinc-200 hover:bg-zinc-900"
                >
                    {refreshing ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
                    Refresh
                </Button>
            </header>

            {error && (
                <div className="rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-200">{error}</div>
            )}

            {loading ? (
                <div className="flex justify-center py-12 text-zinc-400">
                    <Loader2 className="h-8 w-8 animate-spin" />
                </div>
            ) : (
                <>
                    {/* Plans awaiting human approval */}
                    {awaiting.length > 0 && (
                        <section className="space-y-3">
                            <div className="flex items-center gap-2 text-sm font-medium text-amber-200">
                                <ShieldCheck className="h-4 w-4" /> Awaiting your approval
                            </div>
                            <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                                {awaiting.map((incident) => (
                                    <Card key={incident.id} className="border-amber-500/30 bg-amber-500/5 shadow-2xl shadow-black/20">
                                        <CardContent className="flex flex-col gap-3 p-5">
                                            <div className="flex items-center justify-between gap-2">
                                                <Badge variant="outline" className={severityClass(incident.severity)}>{incident.severity}</Badge>
                                                <span className="text-xs text-zinc-500">{incident.clusterName}</span>
                                            </div>
                                            <button className="text-left" onClick={() => openIncident(incident)}>
                                                <div className="font-medium text-white hover:underline">{incident.title}</div>
                                                <div className="mt-1 line-clamp-3 text-xs text-zinc-400">
                                                    {incident.summary || incident.description || "Remediation plan proposed; awaiting approval."}
                                                </div>
                                            </button>
                                            <div className="mt-auto flex items-center gap-2">
                                                <Button
                                                    onClick={() => void approve(incident.id)}
                                                    disabled={approving === incident.id}
                                                    className="gap-2 bg-amber-500 text-slate-950 hover:bg-amber-400"
                                                >
                                                    {approving === incident.id ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}
                                                    Approve plan
                                                </Button>
                                                <Button variant="outline" onClick={() => openIncident(incident)} className="border-zinc-800 bg-zinc-950/60 text-zinc-200 hover:bg-zinc-900">
                                                    Review
                                                </Button>
                                            </div>
                                        </CardContent>
                                    </Card>
                                ))}
                            </div>
                        </section>
                    )}

                    {/* Active investigations (parallel board) */}
                    <section className="space-y-3">
                        <div className="flex items-center gap-2 text-sm font-medium text-cyan-200">
                            <Activity className="h-4 w-4" /> Active investigations
                        </div>
                        {active.length === 0 ? (
                            <Card className="border-zinc-800 bg-zinc-950/70">
                                <CardContent className="flex min-h-[160px] flex-col items-center justify-center gap-2 p-8 text-center text-zinc-400">
                                    <TriangleAlert className="h-6 w-6 text-cyan-300" />
                                    No active investigations right now.
                                </CardContent>
                            </Card>
                        ) : (
                            <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                                {active.map((incident) => (
                                    <Card
                                        key={incident.id}
                                        className="cursor-pointer border-zinc-800 bg-zinc-950/70 shadow-2xl shadow-black/20 transition hover:border-cyan-500/40"
                                        onClick={() => openIncident(incident)}
                                    >
                                        <CardContent className="flex flex-col gap-3 p-5">
                                            <div className="flex items-center justify-between gap-2">
                                                <Badge variant="outline" className={severityClass(incident.severity)}>{incident.severity}</Badge>
                                                <Badge variant="outline" className="border-cyan-500/30 bg-cyan-500/10 text-cyan-200">
                                                    {incident.liveStatus === "RUNNING" ? "investigating" : incident.status}
                                                </Badge>
                                            </div>
                                            <div>
                                                <div className="font-medium text-white">{incident.title}</div>
                                                <div className="mt-1 line-clamp-3 text-xs text-zinc-400">
                                                    {incident.summary || incident.description || "Investigation in progress…"}
                                                </div>
                                            </div>
                                            <div className="mt-auto flex items-center justify-between text-xs text-zinc-500">
                                                <span>{incident.clusterName}</span>
                                                <span>{formatTime(incident.created_at)}</span>
                                            </div>
                                        </CardContent>
                                    </Card>
                                ))}
                            </div>
                        )}
                    </section>

                    {/* Recently resolved */}
                    {resolved.length > 0 && (
                        <section className="space-y-3">
                            <div className="flex items-center gap-2 text-sm font-medium text-emerald-200">
                                <CheckCircle2 className="h-4 w-4" /> Recently resolved
                            </div>
                            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                                {resolved.slice(0, 6).map((incident) => (
                                    <button
                                        key={incident.id}
                                        onClick={() => openIncident(incident)}
                                        className="flex items-center justify-between gap-3 rounded-2xl border border-zinc-800 bg-zinc-950/60 px-4 py-3 text-left hover:border-emerald-500/30"
                                    >
                                        <div className="min-w-0">
                                            <div className="truncate text-sm font-medium text-white">{incident.title}</div>
                                            <div className="truncate text-xs text-zinc-500">{incident.clusterName}</div>
                                        </div>
                                        <Badge variant="outline" className="border-emerald-500/30 bg-emerald-500/10 text-emerald-200">resolved</Badge>
                                    </button>
                                ))}
                            </div>
                        </section>
                    )}
                </>
            )}
        </div>
    )
}
