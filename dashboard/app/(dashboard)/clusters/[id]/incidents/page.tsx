"use client"

import { useCallback, useEffect, useState } from "react"
import { useParams, useRouter } from "next/navigation"
import { ArrowLeft, ChevronRight, Loader2, RefreshCw, Server, TriangleAlert } from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api } from "@/lib/auth-context"

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
    return status.toLowerCase() === "resolved"
        ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-200"
        : "border-cyan-500/30 bg-cyan-500/10 text-cyan-200"
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

export default function ClusterIncidentsPage() {
    const router = useRouter()
    const params = useParams()
    const clusterId = params.id as string
    const [cluster, setCluster] = useState<Cluster | null>(null)
    const [incidents, setIncidents] = useState<Incident[]>([])
    const [loading, setLoading] = useState(true)
    const [refreshing, setRefreshing] = useState(false)
    const [error, setError] = useState<string | null>(null)

    const loadIncidents = useCallback(async (initial = false) => {
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
            setCluster(matchedCluster)
            setIncidents(incidentsResponse.data as Incident[])
        } catch (loadError) {
            setError(loadError instanceof Error ? loadError.message : "Failed to load incidents")
        } finally {
            setRefreshing(false)
            setLoading(false)
        }
    }, [clusterId])

    useEffect(() => {
        void loadIncidents(true)
    }, [loadIncidents])

    useEffect(() => {
        const interval = setInterval(() => {
            void loadIncidents()
        }, 10000)

        return () => clearInterval(interval)
    }, [loadIncidents])

    return (
        <div className="flex w-full min-w-0 flex-1 flex-col gap-6">
            <header className="flex flex-col gap-4 rounded-3xl border border-zinc-800 bg-zinc-950/70 p-5 shadow-2xl shadow-black/20 backdrop-blur md:flex-row md:items-center md:justify-between">
                <div className="flex items-start gap-4">
                    <Button variant="ghost" size="icon" onClick={() => router.push("/")} className="shrink-0 border border-zinc-800 bg-zinc-950/60 text-zinc-300 hover:bg-zinc-900 hover:text-white">
                        <ArrowLeft className="h-5 w-5" />
                    </Button>
                    <div className="space-y-2">
                        <div className="flex flex-wrap items-center gap-3">
                            <h1 className="text-2xl font-semibold tracking-tight text-white md:text-3xl">
                                {cluster?.name || "Incidents"}
                            </h1>
                            <Badge variant="outline" className={cluster?.status === "online" ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-200" : "border-zinc-700 bg-zinc-900 text-zinc-300"}>
                                {cluster?.status || "unknown"}
                            </Badge>
                        </div>
                        <p className="max-w-3xl text-sm text-zinc-400">
                            Incidents for this cluster are listed below. Open one to enter the incident dashboard and continue the investigation thread.
                        </p>
                    </div>
                </div>

                <div className="flex flex-wrap items-center gap-2">
                    <Button
                        variant="outline"
                        onClick={() => void loadIncidents()}
                        disabled={refreshing}
                        className="gap-2 border-zinc-800 bg-zinc-950/60 text-zinc-200 hover:bg-zinc-900"
                    >
                        {refreshing ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
                        Refresh
                    </Button>
                    <Badge variant="outline" className="border-zinc-700 text-zinc-300">
                        {incidents.length} incidents
                    </Badge>
                </div>
            </header>

            {error && (
                <div className="rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-200">
                    {error}
                </div>
            )}

            {loading ? (
                <div className="flex justify-center py-12 text-zinc-400">
                    <Loader2 className="h-8 w-8 animate-spin" />
                </div>
            ) : incidents.length === 0 ? (
                <Card className="border-zinc-800 bg-zinc-950/70 shadow-2xl shadow-black/20">
                    <CardContent className="flex min-h-[320px] flex-col items-center justify-center gap-4 p-8 text-center">
                        <div className="flex h-14 w-14 items-center justify-center rounded-2xl border border-cyan-500/20 bg-cyan-500/10 text-cyan-300">
                            <TriangleAlert className="h-6 w-6" />
                        </div>
                        <div className="space-y-2">
                            <h2 className="text-xl font-semibold text-white">No incidents yet</h2>
                            <p className="max-w-xl text-sm text-zinc-400">
                                This cluster has not reported any incidents. When alerts arrive, they will appear here as a table of active and historical events.
                            </p>
                        </div>
                        <Button onClick={() => router.push("/")} className="gap-2 bg-cyan-500 text-slate-950 hover:bg-cyan-400">
                            Back to clusters
                        </Button>
                    </CardContent>
                </Card>
            ) : (
                <Card className="overflow-hidden border-zinc-800 bg-zinc-950/70 shadow-2xl shadow-black/20">
                    <CardContent className="p-0">
                        <Table>
                            <TableHeader>
                                <TableRow className="border-zinc-800 hover:bg-transparent">
                                    <TableHead className="text-zinc-400">Incident</TableHead>
                                    <TableHead className="text-zinc-400">Severity</TableHead>
                                    <TableHead className="text-zinc-400">Status</TableHead>
                                    <TableHead className="text-zinc-400">Created</TableHead>
                                    <TableHead className="text-zinc-400">Summary</TableHead>
                                    <TableHead className="text-zinc-400" />
                                </TableRow>
                            </TableHeader>
                            <TableBody>
                                {incidents.map((incident) => (
                                    <TableRow
                                        key={incident.id}
                                        className="cursor-pointer border-zinc-800 hover:bg-zinc-900/60"
                                        onClick={() => router.push(`/clusters/${clusterId}/incidents/${incident.id}`)}
                                    >
                                        <TableCell className="max-w-[320px]">
                                            <div className="space-y-1">
                                                <div className="font-medium text-white">{incident.title}</div>
                                                <div className="line-clamp-2 text-xs text-zinc-500">
                                                    {incident.description || "No description captured yet."}
                                                </div>
                                            </div>
                                        </TableCell>
                                        <TableCell>
                                            <Badge variant="outline" className={severityClass(incident.severity)}>
                                                {incident.severity}
                                            </Badge>
                                        </TableCell>
                                        <TableCell>
                                            <Badge variant="outline" className={statusClass(incident.status)}>
                                                {incident.status}
                                            </Badge>
                                        </TableCell>
                                        <TableCell className="text-zinc-300">{formatTime(incident.created_at)}</TableCell>
                                        <TableCell className="max-w-[360px] text-zinc-400">
                                            <div className="line-clamp-2">
                                                {incident.summary || incident.description || "No summary available yet."}
                                            </div>
                                        </TableCell>
                                        <TableCell className="text-right">
                                            <Button variant="ghost" size="icon" className="text-zinc-400 hover:bg-zinc-900 hover:text-white">
                                                <ChevronRight className="h-4 w-4" />
                                            </Button>
                                        </TableCell>
                                    </TableRow>
                                ))}
                            </TableBody>
                        </Table>
                    </CardContent>
                </Card>
            )}
        </div>
    )
}