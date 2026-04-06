"use client"

import { useState } from "react"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Terminal, Shield, Check, X, Play, Loader2 } from "lucide-react"
import Cookies from "js-cookie"

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

interface IncidentCommandCenterProps {
    job: Job
    onRefresh: () => void
}

export function IncidentCommandCenter({ job, onRefresh }: IncidentCommandCenterProps) {
    const [actionLoading, setActionLoading] = useState(false)

    const handleApproval = async (approved: boolean) => {
        setActionLoading(true)
        try {
            const token = Cookies.get("token")
            await fetch(`/api/v1/jobs/${job.id}/approve`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "Authorization": `Bearer ${token}`
                },
                body: JSON.stringify({ approved })
            })
            onRefresh()
        } catch (error) {
            console.error("Failed to submit approval", error)
        } finally {
            setActionLoading(false)
        }
    }

    // Parse job payload and result for dynamic context
    const payload = (() => {
        try { return job.payload ? JSON.parse(job.payload) : {} } catch { return {} }
    })()
    const result = (() => {
        try { return job.result ? JSON.parse(job.result) : {} } catch { return {} }
    })()

    const incidentTitle = payload.alert || payload.title || job.job_type || "Investigation"
    const severity = payload.severity || (job.status === "failed" ? "critical" : "medium")
    const hypothesis = result.hypothesis || result.summary || result.diagnosis || null

    // Calculate duration from job timestamps
    const duration = (() => {
        const start = job.started_at || job.created_at
        const end = job.completed_at || new Date().toISOString()
        const diffMs = new Date(end).getTime() - new Date(start).getTime()
        if (diffMs < 0) return "0s"
        const mins = Math.floor(diffMs / 60000)
        const secs = Math.floor((diffMs % 60000) / 1000)
        return mins > 0 ? `${mins}m ${secs}s` : `${secs}s`
    })()

    // Extract plan steps from result
    const planSteps: string[] = result.plan || result.actions || result.remediation_steps || []

    const severityColor = severity === "critical" ? "border-red-500 text-red-500" :
        severity === "high" ? "border-orange-500 text-orange-500" :
            "border-yellow-500 text-yellow-500"

    return (
        <div className="grid gap-4 md:grid-cols-12 h-[500px]">
            {/* Left: Incident Context */}
            <Card className="md:col-span-3 flex flex-col">
                <CardHeader>
                    <CardTitle className="text-sm font-medium text-muted-foreground">INCIDENT CONTEXT</CardTitle>
                    <div className="flex items-center gap-2">
                        <Badge variant="outline" className={`${job.status === "running" ? "animate-pulse" : ""} ${severityColor}`}>
                            {severity}
                        </Badge>
                        <span className="text-xl font-bold tracking-tight truncate" title={incidentTitle}>
                            {incidentTitle}
                        </span>
                    </div>
                </CardHeader>
                <CardContent className="space-y-4">
                    <div>
                        <p className="text-xs text-muted-foreground uppercase">
                            {hypothesis ? "Hypothesis" : "Status"}
                        </p>
                        <p className="text-sm">
                            {hypothesis || `Job ${job.status}`}
                        </p>
                    </div>
                    <div>
                        <p className="text-xs text-muted-foreground uppercase">Duration</p>
                        <p className="font-mono text-lg">{duration}</p>
                    </div>
                </CardContent>
            </Card>

            {/* Center: Live Terminal */}
            <Card className="md:col-span-6 flex flex-col bg-black border-zinc-800">
                <CardHeader className="py-3 border-b border-zinc-800">
                    <div className="flex items-center gap-2">
                        <Terminal className="h-4 w-4 text-green-500" />
                        <CardTitle className="text-sm font-mono text-zinc-400">AGENT_TERMINAL_OUTPUT</CardTitle>
                    </div>
                </CardHeader>
                <CardContent className="flex-1 p-0 overflow-hidden relative">
                    <ScrollArea className="h-full w-full p-4 font-mono text-xs text-zinc-300">
                        {job.logs ? (
                            job.logs.split('\n').map((line, i) => (
                                <div key={i} className="py-0.5 border-l-2 border-transparent hover:border-zinc-700 pl-2">
                                    <span className="text-zinc-500 mr-2">
                                        {new Date().toLocaleTimeString().split(' ')[0]}
                                    </span>
                                    {line}
                                </div>
                            ))
                        ) : (
                            <div className="flex flex-col items-center justify-center h-full text-zinc-600 gap-2">
                                <Loader2 className="h-8 w-8 animate-spin" />
                                <p>Initializing Agent Runtime...</p>
                            </div>
                        )}
                        {/* Auto-scroll anchor */}
                        <div id="terminal-end" />
                    </ScrollArea>
                </CardContent>
            </Card>

            {/* Right: Action Deck */}
            <Card className="md:col-span-3 flex flex-col bg-slate-900/50 border-slate-800">
                <CardHeader>
                    <CardTitle className="text-sm font-medium text-slate-400">ACTION DECK</CardTitle>
                </CardHeader>
                <CardContent className="flex-1 flex flex-col justify-between">
                    <div className="space-y-4">
                        <div className="p-3 bg-slate-900 rounded border border-slate-700">
                            <div className="flex items-center gap-2 text-blue-400 mb-2">
                                <Shield className="h-4 w-4" />
                                <span className="text-xs font-bold uppercase">
                                    {planSteps.length > 0 ? "Proposed Plan" : "Agent Output"}
                                </span>
                            </div>
                            {planSteps.length > 0 ? (
                                <ul className="text-xs space-y-2 text-slate-300">
                                    {planSteps.map((step: string, i: number) => (
                                        <li key={i} className="flex gap-2">
                                            <span className="text-slate-500">{i + 1}.</span>
                                            <span>{step}</span>
                                        </li>
                                    ))}
                                </ul>
                            ) : (
                                <p className="text-xs text-slate-400">
                                    {job.status === "running" ? "Agent is investigating..." :
                                     job.status === "completed" ? (result.summary || "Investigation complete.") :
                                     job.status === "failed" ? (result.error || "Job failed.") :
                                     "Waiting for agent to start..."}
                                </p>
                            )}
                        </div>
                    </div>

                    <div className="space-y-2 mt-4">
                        {job.status === 'wait_approval' ? (
                            <>
                                <Button
                                    className="w-full bg-green-600 hover:bg-green-700 text-white"
                                    onClick={() => handleApproval(true)}
                                    disabled={actionLoading}
                                >
                                    <Check className="mr-2 h-4 w-4" />
                                    APPROVE PLAN
                                </Button>
                                <Button
                                    variant="outline"
                                    className="w-full border-red-900 text-red-500 hover:bg-red-950 hover:text-red-400"
                                    onClick={() => handleApproval(false)}
                                    disabled={actionLoading}
                                >
                                    <X className="mr-2 h-4 w-4" />
                                    REJECT
                                </Button>
                            </>
                        ) : (
                            <Button variant="ghost" className="w-full text-slate-500 cursor-default" disabled>
                                {job.status === 'running' ? (
                                    <span className="flex items-center animate-pulse">
                                        <Play className="mr-2 h-3 w-3" /> AGENT RUNNING
                                    </span>
                                ) : (
                                    "NO PENDING ACTIONS"
                                )}
                            </Button>
                        )}
                    </div>
                </CardContent>
            </Card>
        </div>
    )
}
