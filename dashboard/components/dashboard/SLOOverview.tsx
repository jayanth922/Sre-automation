"use client"

import { useEffect, useState } from "react"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Activity, Target, Zap } from "lucide-react"
import Cookies from "js-cookie"

interface SLO {
    id: string
    name: string
    target: number
    current_value: number
    error_budget_remaining: number
}

interface SLOOverviewProps {
    clusterId: string
}

export function SLOOverview({ clusterId }: SLOOverviewProps) {
    const [slos, setSlos] = useState<SLO[]>([])
    const [loading, setLoading] = useState(true)

    useEffect(() => {
        const fetchSLOs = async () => {
            try {
                const token = Cookies.get("token")
                const res = await fetch(`/api/v1/clusters/${clusterId}/slos`, {
                    headers: { "Authorization": `Bearer ${token}` }
                })
                if (res.ok) {
                    const data = await res.json()
                    setSlos(data)
                }
            } catch (error) {
                console.error("Failed to fetch SLOs", error)
            } finally {
                setLoading(false)
            }
        }

        fetchSLOs()
        const interval = setInterval(fetchSLOs, 15000)
        return () => clearInterval(interval)
    }, [clusterId])

    if (loading && slos.length === 0) return null
    if (!loading && slos.length === 0) return null

    return (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {slos.map((slo) => {
                const isHealthy = (slo.current_value || 100) >= slo.target
                const budgetLow = (slo.error_budget_remaining || 0) < 20

                return (
                    <Card key={slo.id} className="bg-zinc-900/50 border-zinc-800 backdrop-blur-sm overflow-hidden border-l-4 border-l-blue-500">
                        <CardHeader className="py-2 px-4 flex flex-row items-center justify-between">
                            <CardTitle className="text-xs font-bold uppercase tracking-tight text-zinc-400">
                                {slo.name}
                            </CardTitle>
                            <Badge variant="outline" className={`${isHealthy ? "text-green-500 border-green-900" : "text-red-500 border-red-900"} text-[10px]`}>
                                {isHealthy ? "COMPLIANT" : "BREACHING"}
                            </Badge>
                        </CardHeader>
                        <CardContent className="px-4 pb-3">
                            <div className="flex justify-between items-baseline mb-2">
                                <div className="text-2xl font-bold font-mono tracking-tighter">
                                    {(slo.current_value || 100).toFixed(2)}%
                                </div>
                                <div className="text-[10px] text-zinc-500 font-mono">
                                    Target: {slo.target.toFixed(1)}%
                                </div>
                            </div>

                            <div className="space-y-1.5">
                                <div className="flex justify-between text-[10px] font-mono text-zinc-500 uppercase">
                                    <span>Error Budget</span>
                                    <span className={budgetLow ? "text-red-400" : "text-zinc-400"}>
                                        {(slo.error_budget_remaining || 0).toFixed(2)}% left
                                    </span>
                                </div>
                                <div className="h-1.5 w-full bg-zinc-800 rounded-full overflow-hidden">
                                    <div 
                                        className={`h-full ${budgetLow ? "bg-red-500" : "bg-blue-500"} transition-all duration-500`}
                                        style={{ width: `${Math.max(0, Math.min(100, slo.error_budget_remaining || 0))}%` }}
                                    ></div>
                                </div>
                            </div>
                        </CardContent>
                    </Card>
                )
            })}
        </div>
    )
}
