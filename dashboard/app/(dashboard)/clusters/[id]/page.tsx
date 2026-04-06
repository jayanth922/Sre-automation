"use client"

import { useEffect, useState } from "react"
import { useParams, useRouter } from "next/navigation"
import Cookies from "js-cookie"
import { ArrowLeft, Loader2, AlertTriangle, Play, Pause } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { MetricSparklines } from "@/components/dashboard/MetricSparklines"
import { IncidentCommandCenter } from "@/components/dashboard/IncidentCommandCenter"
import { AuditLogTable } from "@/components/dashboard/AuditLogTable"
import { AgentStatus } from "@/components/dashboard/AgentStatus"
import { SLOOverview } from "@/components/dashboard/SLOOverview"

interface Job {
    id: string
    job_type: string
    status: string
    payload: string | null
    result: string | null
    logs: string | null
    created_at: string
    started_at: string | null
    completed_at: string | null
}

interface Cluster {
    id: string
    name: string
    status: string
}

const EMPTY_METRIC = { latency: 0, errors: 0, cpu: 0, mem: 0 }

export default function ClusterDetailsPage() {
    const router = useRouter()
    const params = useParams()
    const clusterId = params.id as string

    const [cluster, setCluster] = useState<Cluster | null>(null)
    const [jobs, setJobs] = useState<Job[]>([])
    const [loading, setLoading] = useState(true)
    const [activeJob, setActiveJob] = useState<Job | null>(null)
    const [sparklineData, setSparklineData] = useState<any[]>([])

    const [locked, setLocked] = useState(false)
    const [lockLoading, setLockLoading] = useState(false)
    const [triggerLoading, setTriggerLoading] = useState(false)

    const getToken = () => Cookies.get("token")

    const fetchMetrics = async () => {
        try {
            const token = getToken()
            const res = await fetch(`/metrics/snapshot?cluster_id=${clusterId}`, {
                headers: { "Authorization": `Bearer ${token}` }
            })
            if (res.ok) {
                const data = await res.json()
                setSparklineData(prev => {
                    const next = [...prev, {
                        latency: data.latency ?? 0,
                        errors: data.errors ?? 0,
                        cpu: data.cpu ?? 0,
                        mem: data.mem ?? 0
                    }]
                    return next.length > 30 ? next.slice(-30) : next
                })
            }
        } catch (error) {
            console.error("Failed to fetch metrics")
        }
    }

    // Core Polling Loop
    useEffect(() => {
        fetchClusterAndJobs()
        fetchLockStatus()
        fetchMetrics()

        const interval = setInterval(() => {
            fetchJobs()
            fetchLockStatus()
            fetchMetrics()
        }, 5000)

        return () => clearInterval(interval)
    }, [clusterId])

    const fetchLockStatus = async () => {
        try {
            const token = getToken()
            const res = await fetch(`/api/v1/clusters/${clusterId}/lock`, {
                headers: { "Authorization": `Bearer ${token}` }
            })
            if (res.ok) {
                const data = await res.json()
                setLocked(data.locked)
            }
        } catch (error) {
            console.error("Failed to fetch lock status")
        }
    }

    const toggleLock = async () => {
        setLockLoading(true)
        try {
            const token = getToken()
            const res = await fetch(`/api/v1/clusters/${clusterId}/lock`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "Authorization": `Bearer ${token}`
                },
                body: JSON.stringify({ locked: !locked })
            })
            if (res.ok) {
                const data = await res.json()
                setLocked(data.locked)
            }
        } catch (error) {
            console.error("Failed to toggle lock")
        } finally {
            setLockLoading(false)
        }
    }

    const fetchClusterAndJobs = async () => {
        try {
            const token = getToken()
            const clustersRes = await fetch("/api/v1/clusters", {
                headers: { "Authorization": `Bearer ${token}` }
            })
            if (clustersRes.ok) {
                const clusters = await clustersRes.json()
                const found = clusters.find((c: Cluster) => c.id === clusterId)
                setCluster(found || null)
            }
            await fetchJobs()
        } catch (error) {
            console.error("Failed to fetch cluster", error)
        } finally {
            setLoading(false)
        }
    }

    // Determine the "Active" job to show in the Command Center
    const fetchJobs = async () => {
        try {
            const token = getToken()
            const res = await fetch(`/api/v1/clusters/${clusterId}/jobs`, {
                headers: { "Authorization": `Bearer ${token}` }
            })
            if (res.ok) {
                const data = await res.json()
                setJobs(data)

                // Prioritize running or waiting_approval jobs
                const active = data.find((j: Job) =>
                    j.status === 'running' || j.status === 'wait_approval'
                ) || data[0] // fallback to most recent

                setActiveJob(active || null)
            }
        } catch (error) {
            console.error("Failed to fetch jobs", error)
        }
    }

    const handleTriggerInvestigation = async () => {
        if (locked) return
        setTriggerLoading(true)
        try {
            const token = getToken()
            const res = await fetch(`/api/v1/clusters/${clusterId}/jobs/trigger`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "Authorization": `Bearer ${token}`
                },
                body: JSON.stringify({
                    job_type: "investigation",
                    payload: JSON.stringify({ alert: "Manual Investigation", triggered_by: "dashboard" })
                })
            })
            if (res.ok) {
                await fetchJobs()
            }
        } finally {
            setTriggerLoading(false)
        }
    }

    if (loading) {
        return (
            <div className="flex h-screen items-center justify-center bg-black">
                <Loader2 className="h-12 w-12 animate-spin text-zinc-500" />
            </div>
        )
    }

    return (
        <div className="min-h-screen bg-zinc-950 text-zinc-50 p-6 flex flex-col gap-6 font-sans antialiased">
            {/* 1. Header & Emergency Controls */}
            <header className="flex justify-between items-start">
                <div className="flex items-center gap-4">
                    <Button variant="ghost" size="icon" onClick={() => router.push("/")} className="text-zinc-500 hover:text-white">
                        <ArrowLeft className="h-5 w-5" />
                    </Button>
                    <div>
                        <div className="flex items-center gap-3">
                            <h1 className="text-2xl font-bold tracking-tight">
                                {cluster?.name || "Production Cluster"}
                            </h1>
                            {locked ? (
                                <Badge variant="destructive" className="animate-pulse bg-red-600/20 text-red-500 border-red-900 border">
                                    ⛔ LOCKED
                                </Badge>
                            ) : (
                                <Badge variant="outline" className="bg-green-900/10 text-green-500 border-green-900">
                                    ● ACTIVE
                                </Badge>
                            )}
                        </div>
                        <p className="text-zinc-500 text-xs mt-1 font-mono">ID: {clusterId}</p>
                    </div>
                </div>

                <div className="flex items-center gap-3">
                    <Button
                        variant={locked ? "secondary" : "destructive"}
                        onClick={toggleLock}
                        disabled={lockLoading}
                        className={locked ? "bg-zinc-800 text-zinc-300" : "bg-red-900/50 hover:bg-red-900 border border-red-800 text-red-500"}
                    >
                        {lockLoading ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <AlertTriangle className="mr-2 h-4 w-4" />}
                        {locked ? "RESUME OPERATION" : "EMERGENCY STOP"}
                    </Button>

                    <Button
                        className="bg-blue-600 hover:bg-blue-700 text-white font-medium"
                        onClick={handleTriggerInvestigation}
                        disabled={triggerLoading || locked}
                    >
                        {triggerLoading ? (
                            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                        ) : (
                            <Play className="mr-2 h-4 w-4" />
                        )}
                        TRIGGER INVESTIGATION
                    </Button>
                </div>
            </header>

            {/* 1.1 Agent Status Bar */}
            <AgentStatus clusterId={clusterId} />

            {/* 2. Vital Signs (Top Pane) */}
            <section>
                <MetricSparklines data={sparklineData} />
            </section>

            {/* 2.1 SLO Overview */}
            <section>
                <div className="flex items-center gap-2 mb-4">
                    <h2 className="text-sm font-bold uppercase tracking-wider text-zinc-500">Service Level Objectives</h2>
                    <div className="h-px flex-1 bg-zinc-800"></div>
                </div>
                <SLOOverview clusterId={clusterId} />
            </section>

            {/* 3. Command Center (Middle Pane) */}
            {activeJob ? (
                <section>
                    <IncidentCommandCenter job={activeJob} onRefresh={fetchJobs} />
                </section>
            ) : (
                <div className="h-[400px] flex items-center justify-center border border-dashed border-zinc-800 rounded-lg text-zinc-600">
                    <p>No Active Incidents. System nominal.</p>
                </div>
            )}

            {/* 4. Audit Log (Bottom Pane) */}
            <section>
                <AuditLogTable />
            </section>
        </div>
    )
}
