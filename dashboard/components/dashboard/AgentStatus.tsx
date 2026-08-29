"use client"

import { useEffect, useState } from "react"
import { Card, CardContent } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Wifi, WifiOff, Clock, ShieldCheck } from "lucide-react"
import Cookies from "js-cookie"

interface AgentStatusProps {
    clusterId: string
}

export function AgentStatus({ clusterId }: AgentStatusProps) {
    const [status, setStatus] = useState<string>("offline")
    const [lastHeartbeat, setLastHeartbeat] = useState<string | null>(null)
    const [heartbeatSource, setHeartbeatSource] = useState<string | null>(null)
    const [heartbeatReason, setHeartbeatReason] = useState<string | null>(null)
    const [ageSeconds, setAgeSeconds] = useState<number | null>(null)
    const [loading, setLoading] = useState(true)

    useEffect(() => {
        const fetchHealth = async () => {
            try {
                const token = Cookies.get("token")
                const res = await fetch(`/api/v1/clusters/${clusterId}/health`, {
                    headers: { "Authorization": `Bearer ${token}` }
                })
                if (res.ok) {
                    const data = await res.json()
                    setStatus(String(data.status || "offline").toLowerCase())
                    setLastHeartbeat(data.last_heartbeat)
                    setHeartbeatSource(data.heartbeat_source ?? null)
                    setHeartbeatReason(data.heartbeat_reason ?? null)
                    setAgeSeconds(
                        typeof data.age_seconds === "number" ? data.age_seconds : null
                    )
                }
            } catch (error) {
                console.error("Failed to fetch agent health", error)
            } finally {
                setLoading(false)
            }
        }

        fetchHealth()
        const interval = setInterval(fetchHealth, 10000)
        return () => clearInterval(interval)
    }, [clusterId])

    const normalized = status.toLowerCase()
    const isOnline = normalized === "online"
    const isDegraded = normalized === "degraded"
    const label =
        normalized === "online"
            ? "ONLINE"
            : normalized === "degraded"
              ? "DEGRADED"
              : normalized === "stale"
                ? "STALE"
                : normalized === "maintenance"
                  ? "MAINTENANCE"
                  : "OFFLINE"

    return (
        <Card className="bg-zinc-900/50 border-zinc-800 backdrop-blur-sm">
            <CardContent className="p-3 flex items-center justify-between">
                <div className="flex items-center gap-4">
                    <div className="flex items-center gap-2">
                        {isOnline || isDegraded ? (
                            <div className="relative">
                                <Wifi className={`h-4 w-4 ${isOnline ? "text-green-500" : "text-amber-500"}`} />
                                <span className="absolute -top-1 -right-1 flex h-2 w-2">
                                    <span className={`animate-ping absolute inline-flex h-full w-full rounded-full opacity-75 ${isOnline ? "bg-green-400" : "bg-amber-400"}`}></span>
                                    <span className={`relative inline-flex rounded-full h-2 w-2 ${isOnline ? "bg-green-500" : "bg-amber-500"}`}></span>
                                </span>
                            </div>
                        ) : (
                            <WifiOff className="h-4 w-4 text-zinc-500" />
                        )}
                        <span className="text-xs font-bold uppercase tracking-wider text-zinc-400">
                            Edge Agent Status
                        </span>
                    </div>

                    <Badge
                        variant={isOnline ? "outline" : "secondary"}
                        className={
                            isOnline
                                ? "border-green-900 text-green-500 bg-green-950/20"
                                : isDegraded
                                  ? "border-amber-900 text-amber-500 bg-amber-950/20"
                                  : "bg-zinc-800 text-zinc-500"
                        }
                    >
                        {label}
                    </Badge>

                    {lastHeartbeat && (
                        <div className="flex items-center gap-1.5 text-zinc-500 text-[10px] font-mono">
                            <Clock className="h-3 w-3" />
                            Last Heartbeat: {new Date(lastHeartbeat).toLocaleTimeString()}
                            {heartbeatSource ? ` · ${heartbeatSource}` : ""}
                            {ageSeconds != null ? ` · ${Math.round(ageSeconds)}s ago` : ""}
                            {heartbeatReason ? ` · ${heartbeatReason}` : ""}
                        </div>
                    )}
                </div>

                <div className="flex items-center gap-3">
                    <div className="flex items-center gap-1 text-[10px] font-bold text-zinc-500 uppercase">
                        <ShieldCheck className="h-3 w-3" />
                        Secure MCP Relay
                    </div>
                </div>
            </CardContent>
        </Card>
    )
}
