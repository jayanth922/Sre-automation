"use client"

import { useEffect, useState } from "react"
import { useParams, useRouter } from "next/navigation"
import { ArrowLeft, TrendingUp, Clock, CheckCircle2, AlertTriangle, Sparkles, RefreshCw } from "lucide-react"
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

    // Recommendations state
    const [recs, setRecs] = useState<string | null>(null)
    const [recsLoading, setRecsLoading] = useState(false)
    const [recsFetched, setRecsFetched] = useState(false)

    const fetchRecommendations = () => {
        setRecsLoading(true)
        api.get(`/clusters/${clusterId}/recommendations`)
            .then((res) => setRecs((res.data as any).recommendations))
            .catch(() => setRecs("Failed to generate recommendations. Please try again."))
            .finally(() => { setRecsLoading(false); setRecsFetched(true) })
    }

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

            {/* AI Recommendations */}
            <Card className="border-zinc-800 bg-zinc-900/60">
                <CardHeader className="pb-2">
                    <div className="flex items-center justify-between">
                        <div className="flex items-center gap-2">
                            <Sparkles className="h-4 w-4 text-violet-400" />
                            <CardTitle className="text-sm font-medium text-zinc-300">
                                AI Reliability Recommendations
                            </CardTitle>
                        </div>
                        <Button
                            size="sm"
                            variant="outline"
                            onClick={fetchRecommendations}
                            disabled={recsLoading}
                            className="gap-1.5 border-zinc-700 bg-zinc-900 text-zinc-300 hover:bg-zinc-800 hover:text-white text-xs"
                        >
                            {recsLoading
                                ? <><RefreshCw className="h-3 w-3 animate-spin" /> Generating…</>
                                : recsFetched
                                    ? <><RefreshCw className="h-3 w-3" /> Regenerate</>
                                    : <><Sparkles className="h-3 w-3 text-violet-400" /> Generate</>}
                        </Button>
                    </div>
                </CardHeader>
                <CardContent>
                    {!recsFetched && !recsLoading && (
                        <div className="flex flex-col items-center justify-center gap-3 py-8 text-center">
                            <div className="rounded-full border border-violet-500/20 bg-violet-500/10 p-3">
                                <Sparkles className="h-5 w-5 text-violet-400" />
                            </div>
                            <p className="text-sm text-zinc-400">
                                Click <span className="text-violet-300 font-medium">Generate</span> to get AI-powered recommendations based on this cluster&apos;s incident history.
                            </p>
                        </div>
                    )}
                    {recsLoading && (
                        <div className="flex items-center gap-3 py-8 justify-center text-zinc-400 text-sm">
                            <RefreshCw className="h-4 w-4 animate-spin text-violet-400" />
                            Analyzing incident patterns…
                        </div>
                    )}
                    {recs && !recsLoading && (
                        <div className="space-y-2 pt-1">
                            {recs.split("\n").filter(l => l.trim()).map((line, i) => (
                                <div key={i} className="flex gap-3 rounded-lg border border-zinc-800 bg-zinc-900/80 px-4 py-3">
                                    <span className="mt-0.5 shrink-0 text-violet-400 text-xs font-bold">{String(i + 1).padStart(2, "0")}</span>
                                    <p className="text-sm text-zinc-300 leading-relaxed"
                                        dangerouslySetInnerHTML={{
                                            __html: line
                                                .replace(/\*\*(.+?)\*\*/g, '<span class="font-semibold text-violet-300">$1</span>')
                                                .replace(/^[-•]\s*/, "")
                                        }}
                                    />
                                </div>
                            ))}
                        </div>
                    )}
                </CardContent>
            </Card>
        </div>
    )
}
