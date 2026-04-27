"use client"

import { useEffect, useState } from "react"
import { useParams, useRouter } from "next/navigation"
import { ArrowLeft, TrendingUp, Clock, CheckCircle2, AlertTriangle } from "lucide-react"
import {
    BarChart, Bar, LineChart, Line,
    XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Cell,
} from "recharts"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { api } from "@/lib/auth-context"

interface Stats {
    total_incidents: number
    resolved: number
    resolution_rate_pct: number
    mttr_minutes: number
}

interface AnalyticsData {
    cluster_name: string
    weekly_incidents: { week: string; count: number }[]
    severity_distribution: { severity: string; count: number }[]
    stats: Stats
    top_alerts: { title: string; count: number }[]
}

const SEVERITY_COLORS: Record<string, string> = {
    Critical: "#f43f5e",
    High: "#f97316",
    Medium: "#eab308",
    Low: "#22d3ee",
}

function StatCard({
    label, value, sub, icon: Icon, color,
}: {
    label: string; value: string | number; sub?: string
    icon: React.ElementType; color: string
}) {
    return (
        <Card className="border-zinc-800 bg-zinc-900/60">
            <CardContent className="flex items-center gap-4 p-5">
                <div className={`rounded-lg p-2 ${color}`}>
                    <Icon className="h-5 w-5" />
                </div>
                <div>
                    <p className="text-xs text-zinc-500 uppercase tracking-wide">{label}</p>
                    <p className="text-2xl font-bold text-white">{value}</p>
                    {sub && <p className="text-xs text-zinc-400 mt-0.5">{sub}</p>}
                </div>
            </CardContent>
        </Card>
    )
}

export default function AnalyticsPage() {
    const params = useParams()
    const router = useRouter()
    const clusterId = params.id as string
    const [data, setData] = useState<AnalyticsData | null>(null)
    const [loading, setLoading] = useState(true)
    const [error, setError] = useState<string | null>(null)

    useEffect(() => {
        api.get(`/clusters/${clusterId}/analytics`)
            .then((res) => setData(res.data as AnalyticsData))
            .catch(() => setError("Failed to load analytics"))
            .finally(() => setLoading(false))
    }, [clusterId])

    if (loading) {
        return (
            <div className="flex h-64 items-center justify-center text-zinc-500 text-sm">
                Loading analytics…
            </div>
        )
    }

    if (error || !data) {
        return (
            <div className="flex h-64 items-center justify-center text-rose-400 text-sm">
                {error ?? "No data"}
            </div>
        )
    }

    const { stats, weekly_incidents, severity_distribution, top_alerts, cluster_name } = data

    return (
        <div className="space-y-6">
            {/* Back nav */}
            <div className="flex items-center gap-3">
                <Button
                    variant="ghost"
                    size="sm"
                    className="text-zinc-400 hover:text-white"
                    onClick={() => router.push(`/clusters/${clusterId}/incidents`)}
                >
                    <ArrowLeft className="mr-1 h-4 w-4" />
                    {cluster_name}
                </Button>
                <span className="text-zinc-600">/</span>
                <span className="text-sm text-zinc-300">Analytics</span>
            </div>

            {/* Stat cards */}
            <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
                <StatCard
                    label="Total Incidents"
                    value={stats.total_incidents}
                    sub="all time"
                    icon={AlertTriangle}
                    color="bg-rose-500/10 text-rose-400"
                />
                <StatCard
                    label="Resolved"
                    value={stats.resolved}
                    sub={`${stats.resolution_rate_pct}% rate`}
                    icon={CheckCircle2}
                    color="bg-emerald-500/10 text-emerald-400"
                />
                <StatCard
                    label="MTTR"
                    value={`${stats.mttr_minutes}m`}
                    sub="mean time to resolve"
                    icon={Clock}
                    color="bg-cyan-500/10 text-cyan-400"
                />
                <StatCard
                    label="This Period"
                    value={weekly_incidents.reduce((s, w) => s + w.count, 0)}
                    sub="last 12 weeks"
                    icon={TrendingUp}
                    color="bg-violet-500/10 text-violet-400"
                />
            </div>

            {/* Charts row */}
            <div className="grid gap-4 md:grid-cols-2">
                {/* Weekly trend */}
                <Card className="border-zinc-800 bg-zinc-900/60">
                    <CardHeader className="pb-2">
                        <CardTitle className="text-sm font-medium text-zinc-300">
                            Weekly Incident Trend
                        </CardTitle>
                    </CardHeader>
                    <CardContent>
                        {weekly_incidents.length === 0 ? (
                            <p className="text-sm text-zinc-500 py-8 text-center">No data yet</p>
                        ) : (
                            <ResponsiveContainer width="100%" height={220}>
                                <LineChart data={weekly_incidents}>
                                    <CartesianGrid strokeDasharray="3 3" stroke="#27272a" />
                                    <XAxis
                                        dataKey="week"
                                        tick={{ fill: "#71717a", fontSize: 11 }}
                                        axisLine={false}
                                        tickLine={false}
                                    />
                                    <YAxis
                                        allowDecimals={false}
                                        tick={{ fill: "#71717a", fontSize: 11 }}
                                        axisLine={false}
                                        tickLine={false}
                                    />
                                    <Tooltip
                                        contentStyle={{ backgroundColor: "#18181b", border: "1px solid #3f3f46", borderRadius: 8 }}
                                        labelStyle={{ color: "#a1a1aa" }}
                                        itemStyle={{ color: "#22d3ee" }}
                                    />
                                    <Line
                                        type="monotone"
                                        dataKey="count"
                                        stroke="#22d3ee"
                                        strokeWidth={2}
                                        dot={{ fill: "#22d3ee", r: 3 }}
                                        activeDot={{ r: 5 }}
                                    />
                                </LineChart>
                            </ResponsiveContainer>
                        )}
                    </CardContent>
                </Card>

                {/* Severity distribution */}
                <Card className="border-zinc-800 bg-zinc-900/60">
                    <CardHeader className="pb-2">
                        <CardTitle className="text-sm font-medium text-zinc-300">
                            Severity Distribution
                        </CardTitle>
                    </CardHeader>
                    <CardContent>
                        {severity_distribution.length === 0 ? (
                            <p className="text-sm text-zinc-500 py-8 text-center">No data yet</p>
                        ) : (
                            <ResponsiveContainer width="100%" height={220}>
                                <BarChart data={severity_distribution}>
                                    <CartesianGrid strokeDasharray="3 3" stroke="#27272a" />
                                    <XAxis
                                        dataKey="severity"
                                        tick={{ fill: "#71717a", fontSize: 11 }}
                                        axisLine={false}
                                        tickLine={false}
                                    />
                                    <YAxis
                                        allowDecimals={false}
                                        tick={{ fill: "#71717a", fontSize: 11 }}
                                        axisLine={false}
                                        tickLine={false}
                                    />
                                    <Tooltip
                                        contentStyle={{ backgroundColor: "#18181b", border: "1px solid #3f3f46", borderRadius: 8 }}
                                        labelStyle={{ color: "#a1a1aa" }}
                                    />
                                    <Bar dataKey="count" radius={[4, 4, 0, 0]}>
                                        {severity_distribution.map((entry) => (
                                            <Cell
                                                key={entry.severity}
                                                fill={SEVERITY_COLORS[entry.severity] ?? "#6366f1"}
                                            />
                                        ))}
                                    </Bar>
                                </BarChart>
                            </ResponsiveContainer>
                        )}
                    </CardContent>
                </Card>
            </div>

            {/* Top recurring alerts */}
            <Card className="border-zinc-800 bg-zinc-900/60">
                <CardHeader className="pb-2">
                    <CardTitle className="text-sm font-medium text-zinc-300">
                        Top Recurring Alerts
                    </CardTitle>
                </CardHeader>
                <CardContent>
                    {top_alerts.length === 0 ? (
                        <p className="text-sm text-zinc-500 py-4 text-center">No recurring alerts</p>
                    ) : (
                        <div className="space-y-2">
                            {top_alerts.map((alert, i) => (
                                <div key={i} className="flex items-center justify-between rounded-lg border border-zinc-800 bg-zinc-900 px-4 py-2">
                                    <span className="text-sm text-zinc-300 truncate max-w-[80%]">{alert.title}</span>
                                    <span className="ml-2 shrink-0 rounded-full bg-zinc-700 px-2 py-0.5 text-xs text-zinc-300">
                                        {alert.count}x
                                    </span>
                                </div>
                            ))}
                        </div>
                    )}
                </CardContent>
            </Card>
        </div>
    )
}
