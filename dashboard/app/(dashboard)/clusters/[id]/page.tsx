"use client"

import { useCallback, useEffect, useState } from "react"
import { useParams, useRouter } from "next/navigation"
import { ArrowLeft, BadgeInfo, ChevronLeft, ChevronRight, Loader2, RefreshCw, Server, ShieldCheck, Trash2 } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent } from "@/components/ui/card"
import {
    Dialog,
    DialogContent,
    DialogDescription,
    DialogHeader,
    DialogTitle,
    DialogTrigger,
} from "@/components/ui/dialog"
import { ScrollArea } from "@/components/ui/scroll-area"
import { IncidentCommandCenter } from "@/components/dashboard/IncidentCommandCenter"
import { api, useAuth } from "@/lib/auth-context"

interface Cluster {
    id: string
    name: string
    status: string
    last_heartbeat?: string | null
    created_at?: string | null
    prometheus_url?: string | null
    loki_url?: string | null
    k8s_api_server?: string | null
    github_repo?: string | null
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

function statusClass(status: string) {
    switch (status.toLowerCase()) {
        case "online":
            return "border-emerald-500/30 bg-emerald-500/10 text-emerald-200"
        case "maintenance":
            return "border-amber-500/30 bg-amber-500/10 text-amber-200"
        case "offline":
            return "border-rose-500/30 bg-rose-500/10 text-rose-200"
        default:
            return "border-zinc-500/30 bg-zinc-500/10 text-zinc-200"
    }
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
    return date.toLocaleString([], {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
    })
}

const INCIDENTS_PER_PAGE = 6

export default function ClusterDetailsPage() {
    const router = useRouter()
    const params = useParams()
    const clusterId = params.id as string
    const { user } = useAuth()

    const [cluster, setCluster] = useState<Cluster | null>(null)
    const [incidents, setIncidents] = useState<Incident[]>([])
    const [selectedIncidentId, setSelectedIncidentId] = useState<string | null>(null)
    const [loading, setLoading] = useState(true)
    const [refreshing, setRefreshing] = useState(false)
    const [dialogOpen, setDialogOpen] = useState(false)
    const [incidentPage, setIncidentPage] = useState(0)
    const [clearingIncidents, setClearingIncidents] = useState(false)
    const [conversationRefreshNonce, setConversationRefreshNonce] = useState(0)

    const selectedIncident = incidents.find((incident: Incident) => incident.id === selectedIncidentId) || null
    const hasIncidents = incidents.length > 0
    const selectedIncidentPosition = selectedIncident ? incidents.findIndex((incident) => incident.id === selectedIncident.id) + 1 : 0
    const totalIncidentPages = Math.max(1, Math.ceil(incidents.length / INCIDENTS_PER_PAGE))
    const paginatedIncidents = incidents.slice(
        incidentPage * INCIDENTS_PER_PAGE,
        incidentPage * INCIDENTS_PER_PAGE + INCIDENTS_PER_PAGE,
    )
    const clusterModeLabel = hasIncidents ? (selectedIncident?.status === "resolved" ? "Review mode" : "Investigation mode") : "Monitoring mode"

    const loadDashboard = useCallback(async (initial = false) => {
        if (initial) {
            setLoading(true)
        }
        setRefreshing(true)

        try {
            const [clustersResponse, incidentsResponse] = await Promise.all([
                api.get("/clusters"),
                api.get(`/clusters/${clusterId}/incidents`),
            ])

            const foundCluster = (clustersResponse.data as Cluster[]).find((item) => item.id === clusterId) || null
            const fetchedIncidents = incidentsResponse.data as Incident[]

            setCluster(foundCluster)
            setIncidents(fetchedIncidents)
        } catch (error) {
            console.error("Failed to load cluster dashboard", error)
            setCluster(null)
            setIncidents([])
        } finally {
            setRefreshing(false)
            setLoading(false)
        }
    }, [clusterId])

    const clearIncidents = async () => {
        if (clearingIncidents) return

        const confirmed = window.confirm("Delete all incidents for this cluster? This cannot be undone.")
        if (!confirmed) return

        setClearingIncidents(true)
        try {
            await api.delete(`/clusters/${clusterId}/incidents`)
            setSelectedIncidentId(null)
            setIncidentPage(0)
            await loadDashboard()
        } catch (error) {
            console.error("Failed to clear incidents", error)
        } finally {
            setClearingIncidents(false)
        }
    }

    const handleRefreshDashboard = () => {
        setConversationRefreshNonce((currentNonce) => currentNonce + 1)
        void loadDashboard()
    }

    useEffect(() => {
        void loadDashboard(true)
        const interval = setInterval(() => {
            void loadDashboard()
        }, 10000)

        return () => {
            clearInterval(interval)
        }
    }, [loadDashboard])

    useEffect(() => {
        if (!selectedIncidentId && incidents.length > 0) {
            setSelectedIncidentId(incidents[0].id)
        } else if (selectedIncidentId && !incidents.find((incident: Incident) => incident.id === selectedIncidentId)) {
            setSelectedIncidentId(incidents[0]?.id ?? null)
        }
    }, [incidents, selectedIncidentId])

    useEffect(() => {
        setIncidentPage((currentPage) => Math.min(currentPage, totalIncidentPages - 1))
    }, [totalIncidentPages])

    useEffect(() => {
        if (!selectedIncidentId) return

        const selectedIndex = incidents.findIndex((incident: Incident) => incident.id === selectedIncidentId)
        if (selectedIndex < 0) return

        const nextPage = Math.floor(selectedIndex / INCIDENTS_PER_PAGE)
        setIncidentPage((currentPage) => (currentPage === nextPage ? currentPage : nextPage))
    }, [incidents, selectedIncidentId])

    const clusterInfoDialog = (
        <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
            <DialogTrigger asChild>
                <Button variant="outline" className="gap-2 border-zinc-800 bg-zinc-950/60 text-zinc-200 hover:bg-zinc-900">
                    <BadgeInfo className="h-4 w-4" />
                    Cluster Info
                </Button>
            </DialogTrigger>
            <DialogContent className="max-w-2xl border-zinc-800 bg-zinc-950 text-zinc-100">
                <DialogHeader>
                    <DialogTitle className="text-white">Cluster and Ownership Details</DialogTitle>
                    <DialogDescription className="text-zinc-400">
                        Operational metadata for the selected cluster and the signed-in owner context.
                    </DialogDescription>
                </DialogHeader>

                <div className="grid gap-4 md:grid-cols-2">
                    <div className="rounded-2xl border border-zinc-800 bg-zinc-900/50 p-4">
                        <div className="mb-3 flex items-center gap-2 text-xs uppercase tracking-[0.24em] text-zinc-500">
                            <Server className="h-3.5 w-3.5 text-cyan-400" />
                            Cluster
                        </div>
                        <div className="space-y-3 text-sm text-zinc-300">
                            <div>
                                <p className="text-xs uppercase tracking-[0.24em] text-zinc-500">Name</p>
                                <p className="mt-1 font-medium text-zinc-100">{cluster?.name || "Unknown cluster"}</p>
                            </div>
                            <div>
                                <p className="text-xs uppercase tracking-[0.24em] text-zinc-500">Status</p>
                                <Badge variant="outline" className={`mt-1 ${cluster ? statusClass(cluster.status) : "border-zinc-700 bg-zinc-900 text-zinc-300"}`}>
                                    {cluster?.status || "unknown"}
                                </Badge>
                            </div>
                            <div>
                                <p className="text-xs uppercase tracking-[0.24em] text-zinc-500">Cluster ID</p>
                                <p className="mt-1 font-mono text-xs text-zinc-400">{cluster?.id || clusterId}</p>
                            </div>
                            <div>
                                <p className="text-xs uppercase tracking-[0.24em] text-zinc-500">Created</p>
                                <p className="mt-1 font-mono text-xs text-zinc-400">{formatTime(cluster?.created_at)}</p>
                            </div>
                            <div>
                                <p className="text-xs uppercase tracking-[0.24em] text-zinc-500">Last Heartbeat</p>
                                <p className="mt-1 font-mono text-xs text-zinc-400">{formatTime(cluster?.last_heartbeat)}</p>
                            </div>
                        </div>
                    </div>

                    <div className="rounded-2xl border border-zinc-800 bg-zinc-900/50 p-4">
                        <div className="mb-3 flex items-center gap-2 text-xs uppercase tracking-[0.24em] text-zinc-500">
                            <ShieldCheck className="h-3.5 w-3.5 text-cyan-400" />
                            Ownership
                        </div>
                        <div className="space-y-3 text-sm text-zinc-300">
                            <div>
                                <p className="text-xs uppercase tracking-[0.24em] text-zinc-500">Signed-in User</p>
                                <p className="mt-1 text-zinc-100">{user?.email || "Unknown user"}</p>
                            </div>
                            <div>
                                <p className="text-xs uppercase tracking-[0.24em] text-zinc-500">Role</p>
                                <p className="mt-1 text-zinc-100">{user?.role || "member"}</p>
                            </div>
                            <div>
                                <p className="text-xs uppercase tracking-[0.24em] text-zinc-500">Organization</p>
                                <p className="mt-1 font-mono text-xs text-zinc-400">{user?.org_id || "-"}</p>
                            </div>
                            <div>
                                <p className="text-xs uppercase tracking-[0.24em] text-zinc-500">Monitoring Surface</p>
                                <p className="mt-1 text-zinc-100">
                                    The dashboard resolves telemetry for this cluster through the registered SaaS endpoints.
                                </p>
                            </div>
                        </div>
                    </div>
                </div>

                <div className="grid gap-3 rounded-2xl border border-zinc-800 bg-zinc-900/40 p-4 text-sm text-zinc-300 md:grid-cols-2">
                    <div>
                        <p className="text-xs uppercase tracking-[0.24em] text-zinc-500">Prometheus</p>
                        <p className="mt-1 break-all text-zinc-100">{cluster?.prometheus_url || "Not configured"}</p>
                    </div>
                    <div>
                        <p className="text-xs uppercase tracking-[0.24em] text-zinc-500">Loki</p>
                        <p className="mt-1 break-all text-zinc-100">{cluster?.loki_url || "Not configured"}</p>
                    </div>
                    <div>
                        <p className="text-xs uppercase tracking-[0.24em] text-zinc-500">Kubernetes API</p>
                        <p className="mt-1 break-all text-zinc-100">{cluster?.k8s_api_server || "Not configured"}</p>
                    </div>
                    <div>
                        <p className="text-xs uppercase tracking-[0.24em] text-zinc-500">GitHub Repo</p>
                        <p className="mt-1 break-all text-zinc-100">{cluster?.github_repo || "Not configured"}</p>
                    </div>
                </div>
            </DialogContent>
        </Dialog>
    )

    if (loading) {
        return (
            <div className="flex h-screen items-center justify-center bg-zinc-950 text-zinc-400">
                <Loader2 className="h-10 w-10 animate-spin" />
            </div>
        )
    }

    return (
        <div className="h-screen overflow-hidden bg-[radial-gradient(circle_at_top_left,_rgba(34,211,238,0.10),_transparent_32%),linear-gradient(180deg,_#090b14_0%,_#05070f_100%)] text-zinc-50">
            <div className="mx-auto flex h-full w-full max-w-[1800px] flex-col gap-5 p-4 md:p-6">
                <header className="flex flex-col gap-4 rounded-3xl border border-zinc-800 bg-zinc-950/65 px-4 py-4 shadow-2xl shadow-black/20 backdrop-blur md:px-5">
                    <div className="flex flex-wrap items-start justify-between gap-4">
                        <div className="flex items-start gap-4">
                            <Button variant="ghost" size="icon" onClick={() => router.push("/")} className="shrink-0 border border-zinc-800 bg-zinc-950/60 text-zinc-300 hover:bg-zinc-900 hover:text-white">
                                <ArrowLeft className="h-5 w-5" />
                            </Button>
                            <div className="space-y-2">
                                <div className="flex flex-wrap items-center gap-3">
                                    <h1 className="text-2xl font-semibold tracking-tight text-white md:text-3xl">
                                        {cluster?.name || "Cluster Dashboard"}
                                    </h1>
                                    <Badge variant="outline" className={cluster ? statusClass(cluster.status) : "border-zinc-700 bg-zinc-900 text-zinc-300"}>
                                        {cluster?.status || "unknown"}
                                    </Badge>
                                </div>
                                <p className="max-w-3xl text-sm text-zinc-400">
                                    Active incident queue on the left. Open any incident to see the AI reasoning stream and chat with the agent in one place.
                                </p>
                                <div className="flex flex-wrap items-center gap-2 pt-1 text-[11px] uppercase tracking-[0.24em] text-zinc-500">
                                    <span className="rounded-full border border-zinc-800 bg-zinc-950/80 px-3 py-1 text-zinc-300">
                                        Queue {incidents.length}
                                    </span>
                                    <span className="rounded-full border border-zinc-800 bg-zinc-950/80 px-3 py-1 text-zinc-300">
                                        Focus {selectedIncidentPosition ? `${selectedIncidentPosition}/${incidents.length}` : "none"}
                                    </span>
                                    <span className="rounded-full border border-zinc-800 bg-zinc-950/80 px-3 py-1 text-zinc-300">
                                        {clusterModeLabel}
                                    </span>
                                    <span className="rounded-full border border-zinc-800 bg-zinc-950/80 px-3 py-1 text-zinc-300">
                                        Live polling 10s
                                    </span>
                                </div>
                            </div>
                        </div>

                        <div className="flex flex-wrap items-center gap-2">
                            <Button
                                variant="outline"
                                onClick={handleRefreshDashboard}
                                disabled={refreshing}
                                className="gap-2 border-zinc-800 bg-zinc-950/60 text-zinc-200 hover:bg-zinc-900"
                            >
                                {refreshing ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
                                Refresh
                            </Button>
                            {clusterInfoDialog}
                        </div>
                    </div>
                </header>

                <main className={hasIncidents ? "grid min-h-0 flex-1 gap-5 lg:grid-cols-[minmax(320px,380px)_minmax(0,1fr)]" : "flex min-h-0 flex-1"}>
                    {hasIncidents ? (
                        <>
                            <section className="flex h-full min-h-0 flex-col rounded-3xl border border-zinc-800 bg-zinc-950/70 shadow-2xl shadow-black/20 backdrop-blur">
                                <div className="border-b border-zinc-800 px-5 py-4">
                                    <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
                                        <div className="min-w-0">
                                            <h2 className="text-sm font-semibold uppercase tracking-[0.28em] text-zinc-400">
                                                Active Incidents
                                            </h2>
                                            <p className="mt-1 text-xs text-zinc-500">
                                                Select a tile to open the incident thread.
                                            </p>
                                        </div>
                                        <div className="flex flex-wrap items-center justify-end gap-2">
                                            {user?.role === "admin" && (
                                                <Button
                                                    variant="outline"
                                                    onClick={() => void clearIncidents()}
                                                    disabled={clearingIncidents || incidents.length === 0}
                                                    className="gap-2 border-rose-500/30 bg-rose-500/10 text-rose-200 hover:bg-rose-500/20 hover:text-rose-100"
                                                >
                                                    {clearingIncidents ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />}
                                                    Clear
                                                </Button>
                                            )}
                                            <Badge variant="outline" className="shrink-0 border-zinc-700 text-zinc-300">
                                                {incidents.length}
                                            </Badge>
                                            <div className="flex items-center gap-1 rounded-full border border-zinc-800 bg-zinc-950/60 p-1 text-zinc-300">
                                                <Button
                                                    variant="ghost"
                                                    size="icon"
                                                    onClick={() => setIncidentPage((page) => Math.max(0, page - 1))}
                                                    disabled={incidentPage === 0}
                                                    className="h-7 w-7 rounded-full text-zinc-400 hover:bg-zinc-900 hover:text-white"
                                                >
                                                    <ChevronLeft className="h-4 w-4" />
                                                </Button>
                                                <span className="min-w-16 px-1 text-center text-[11px] uppercase tracking-[0.24em] text-zinc-500">
                                                    {incidentPage + 1}/{totalIncidentPages}
                                                </span>
                                                <Button
                                                    variant="ghost"
                                                    size="icon"
                                                    onClick={() => setIncidentPage((page) => Math.min(totalIncidentPages - 1, page + 1))}
                                                    disabled={incidentPage >= totalIncidentPages - 1}
                                                    className="h-7 w-7 rounded-full text-zinc-400 hover:bg-zinc-900 hover:text-white"
                                                >
                                                    <ChevronRight className="h-4 w-4" />
                                                </Button>
                                            </div>
                                        </div>
                                    </div>
                                </div>

                                <ScrollArea className="min-h-0 flex-1 p-4">
                                    <div className="space-y-3 pr-2">
                                        {paginatedIncidents.map((incident) => {
                                            const selected = incident.id === selectedIncidentId
                                            return (
                                                <Card
                                                    key={incident.id}
                                                    className={`cursor-pointer border transition-all duration-200 ${selected ? "border-cyan-500/40 bg-cyan-500/5 shadow-lg shadow-cyan-500/10" : "border-zinc-800 bg-zinc-950/50 hover:border-zinc-700 hover:bg-zinc-900/60"}`}
                                                    onClick={() => setSelectedIncidentId(incident.id)}
                                                >
                                                    <CardContent className="p-3.5">
                                                        <div className="space-y-3">
                                                            <div className="flex items-start justify-between gap-3">
                                                                <h3 className="line-clamp-2 text-sm font-semibold leading-5 text-white">
                                                                    {incident.title}
                                                                </h3>
                                                                {selected && <span className="text-[10px] uppercase tracking-[0.24em] text-cyan-300">Selected</span>}
                                                            </div>
                                                            <div className="flex flex-wrap items-center gap-2">
                                                                <Badge variant="outline" className={severityClass(incident.severity)}>
                                                                    {incident.severity}
                                                                </Badge>
                                                                <Badge variant="outline" className={incident.status === "resolved" ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-200" : "border-amber-500/30 bg-amber-500/10 text-amber-200"}>
                                                                    {incident.status}
                                                                </Badge>
                                                            </div>
                                                            <p className="line-clamp-2 text-xs leading-5 text-zinc-400">
                                                                {incident.summary || incident.description || "No extra context captured yet."}
                                                            </p>
                                                            <div className="flex items-center justify-between text-[11px] uppercase tracking-[0.24em] text-zinc-500">
                                                                <span>{formatTime(incident.created_at)}</span>
                                                                <span className="text-zinc-600">{incident.resolved_at ? "Closed" : "Open"}</span>
                                                            </div>
                                                        </div>
                                                    </CardContent>
                                                </Card>
                                            )
                                        })}
                                    </div>
                                </ScrollArea>
                            </section>

                            <section className="h-full min-h-0">
                                        <IncidentCommandCenter incident={selectedIncident} refreshNonce={conversationRefreshNonce} />
                            </section>
                        </>
                    ) : (
                        <section className="flex h-full min-h-0 flex-1 flex-col overflow-hidden rounded-3xl border border-zinc-800 bg-zinc-950/70 shadow-2xl shadow-black/20 backdrop-blur">
                            <div className="border-b border-zinc-800 px-5 py-4">
                                <div className="flex items-center justify-between gap-3">
                                    <div>
                                        <h2 className="text-sm font-semibold uppercase tracking-[0.28em] text-zinc-400">
                                            Active Incidents
                                        </h2>
                                        <p className="mt-1 text-xs text-zinc-500">
                                            The queue is empty right now, so the full surface stays focused on the cluster state.
                                        </p>
                                    </div>
                                    <div className="flex items-center gap-2">
                                        {user?.role === "admin" && (
                                            <Button
                                                variant="outline"
                                                onClick={() => void clearIncidents()}
                                                disabled={clearingIncidents || incidents.length === 0}
                                                className="gap-2 border-rose-500/30 bg-rose-500/10 text-rose-200 hover:bg-rose-500/20 hover:text-rose-100"
                                            >
                                                {clearingIncidents ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />}
                                                Clear
                                            </Button>
                                        )}
                                        <Badge variant="outline" className="border-zinc-700 text-zinc-300">
                                            0
                                        </Badge>
                                        <div className="flex items-center gap-1 rounded-full border border-zinc-800 bg-zinc-950/60 p-1 text-zinc-300">
                                            <Button
                                                variant="ghost"
                                                size="icon"
                                                disabled
                                                className="h-7 w-7 rounded-full text-zinc-500"
                                            >
                                                <ChevronLeft className="h-4 w-4" />
                                            </Button>
                                            <span className="min-w-16 px-1 text-center text-[11px] uppercase tracking-[0.24em] text-zinc-500">
                                                0/1
                                            </span>
                                            <Button
                                                variant="ghost"
                                                size="icon"
                                                disabled
                                                className="h-7 w-7 rounded-full text-zinc-500"
                                            >
                                                <ChevronRight className="h-4 w-4" />
                                            </Button>
                                        </div>
                                    </div>
                                </div>
                            </div>

                            <div className="flex min-h-0 flex-1 items-center justify-center p-8 md:p-12">
                                <div className="w-full max-w-2xl rounded-[28px] border border-dashed border-zinc-800 bg-zinc-950/55 px-6 py-14 text-center shadow-inner shadow-black/20 md:px-10">
                                    <div className="mx-auto mb-5 flex h-14 w-14 items-center justify-center rounded-2xl border border-cyan-500/20 bg-cyan-500/10 text-cyan-300">
                                        <Server className="h-6 w-6" />
                                    </div>
                                    <h3 className="text-2xl font-semibold tracking-tight text-white">
                                        No active incidents
                                    </h3>
                                    <p className="mx-auto mt-3 max-w-xl text-sm leading-6 text-zinc-400">
                                        This cluster is healthy enough that the incident queue is empty. When Alertmanager fires, the list will populate here and the investigation chat will appear on the right.
                                    </p>
                                    <div className="mt-8 flex flex-wrap items-center justify-center gap-3">
                                        <Button
                                            onClick={() => void loadDashboard()}
                                            disabled={refreshing}
                                            className="gap-2 bg-cyan-500 text-slate-950 hover:bg-cyan-400"
                                        >
                                            {refreshing ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
                                            Refresh
                                        </Button>
                                        {clusterInfoDialog}
                                    </div>
                                </div>
                            </div>
                        </section>
                    )}
                </main>
            </div>
        </div>
    )
}
