"use client"

import { useEffect, useState } from "react"
import Link from "next/link"
import { Plus, Server, Trash2, Loader2, Check, Copy } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardFooter, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
    Dialog,
    DialogContent,
    DialogDescription,
    DialogFooter,
    DialogHeader,
    DialogTitle,
    DialogTrigger,
} from "@/components/ui/dialog"
import { useAuth, api } from "@/lib/auth-context"

interface Cluster {
    id: string
    name: string
    status: string
    prometheus_url: string | null
    loki_url: string | null
    github_repo: string | null
}

export default function DashboardHome() {
    const [clusters, setClusters] = useState<Cluster[]>([])
    const [loading, setLoading] = useState(true)
    const [error, setError] = useState<string | null>(null)
    const [open, setOpen] = useState(false)
    const [createError, setCreateError] = useState<string | null>(null)
    const [createdClusterToken, setCreatedClusterToken] = useState<string | null>(null)
    const [copied, setCopied] = useState(false)
    const { user } = useAuth()

    // Form fields
    const [newCluster, setNewCluster] = useState({
        name: "",
        prometheus_url: "",
        loki_url: "",
        k8s_api_server: "",
        github_token: "",
        github_repo: "",
    })

    useEffect(() => {
        fetchClusters()
        const interval = setInterval(fetchClusters, 5000)
        return () => clearInterval(interval)
    }, [])

    const fetchClusters = async () => {
        try {
            const res = await api.get("/clusters")
            setClusters(res.data)
            setError(null)
        } catch (e: any) {
            if (e.response?.status !== 401) {
                setError("Failed to load clusters. Is the API running?")
            }
        } finally {
            setLoading(false)
        }
    }

    const handleCreateCluster = async () => {
        setCreateError(null)
        try {
            const payload: any = { name: newCluster.name }
            if (newCluster.prometheus_url) payload.prometheus_url = newCluster.prometheus_url
            if (newCluster.loki_url) payload.loki_url = newCluster.loki_url
            if (newCluster.k8s_api_server) payload.k8s_api_server = newCluster.k8s_api_server
            if (newCluster.github_token) payload.github_token = newCluster.github_token
            if (newCluster.github_repo) payload.github_repo = newCluster.github_repo

            const res = await api.post("/clusters", payload)
            if (res.status === 200) {
                setCreatedClusterToken(res.data.token)
                fetchClusters()
            }
        } catch (e: any) {
            setCreateError(e.response?.data?.detail || "Failed to create cluster")
        }
    }

    const copyToken = () => {
        if (createdClusterToken) {
            navigator.clipboard.writeText(createdClusterToken)
            setCopied(true)
            setTimeout(() => setCopied(false), 2000)
        }
    }

    const resetModal = () => {
        setOpen(false)
        setCreatedClusterToken(null)
        setCreateError(null)
        setCopied(false)
        setNewCluster({ name: "", prometheus_url: "", loki_url: "", k8s_api_server: "", github_token: "", github_repo: "" })
    }

    const handleDeleteCluster = async (clusterId: string, clusterName: string) => {
        if (!confirm(`Delete cluster "${clusterName}"? This cannot be undone.`)) return
        try {
            await api.delete(`/clusters/${clusterId}`)
            fetchClusters()
        } catch (e: any) {
            alert(e.response?.data?.detail || "Failed to delete cluster.")
        }
    }

    const statusColor = (status: string) => {
        switch (status) {
            case "online": return "bg-green-500 hover:bg-green-600"
            case "maintenance": return "bg-yellow-500 hover:bg-yellow-600"
            default: return "bg-red-500 hover:bg-red-600"
        }
    }

    return (
        <div className="space-y-6">
            <div className="flex justify-between items-center">
                <h1 className="text-3xl font-bold tracking-tight">Clusters</h1>

                <Dialog open={open} onOpenChange={setOpen}>
                    <DialogTrigger asChild>
                        <Button onClick={() => setCreatedClusterToken(null)}>
                            <Plus className="mr-2 h-4 w-4" /> Connect Cluster
                        </Button>
                    </DialogTrigger>
                    <DialogContent className="sm:max-w-[550px]">
                        <DialogHeader>
                            <DialogTitle>Connect a New Cluster</DialogTitle>
                            <DialogDescription>
                                Register your cluster and provide your infrastructure endpoints.
                            </DialogDescription>
                        </DialogHeader>

                        {!createdClusterToken ? (
                            <div className="grid gap-4 py-4">
                                <div className="grid grid-cols-4 items-center gap-4">
                                    <Label htmlFor="name" className="text-right">Name *</Label>
                                    <Input id="name" value={newCluster.name} onChange={e => setNewCluster({ ...newCluster, name: e.target.value })} placeholder="Production US-East" className="col-span-3" />
                                </div>
                                <div className="grid grid-cols-4 items-center gap-4">
                                    <Label htmlFor="prometheus" className="text-right">Prometheus</Label>
                                    <Input id="prometheus" value={newCluster.prometheus_url} onChange={e => setNewCluster({ ...newCluster, prometheus_url: e.target.value })} placeholder="http://prometheus:9090" className="col-span-3" />
                                </div>
                                <div className="grid grid-cols-4 items-center gap-4">
                                    <Label htmlFor="loki" className="text-right">Loki</Label>
                                    <Input id="loki" value={newCluster.loki_url} onChange={e => setNewCluster({ ...newCluster, loki_url: e.target.value })} placeholder="http://loki:3100" className="col-span-3" />
                                </div>
                                <div className="grid grid-cols-4 items-center gap-4">
                                    <Label htmlFor="k8s" className="text-right">K8s API</Label>
                                    <Input id="k8s" value={newCluster.k8s_api_server} onChange={e => setNewCluster({ ...newCluster, k8s_api_server: e.target.value })} placeholder="https://kubernetes.default.svc" className="col-span-3" />
                                </div>
                                <div className="grid grid-cols-4 items-center gap-4">
                                    <Label htmlFor="ghrepo" className="text-right">GitHub Repo</Label>
                                    <Input id="ghrepo" value={newCluster.github_repo} onChange={e => setNewCluster({ ...newCluster, github_repo: e.target.value })} placeholder="org/repo" className="col-span-3" />
                                </div>
                                <div className="grid grid-cols-4 items-center gap-4">
                                    <Label htmlFor="ghtoken" className="text-right">GitHub Token</Label>
                                    <Input id="ghtoken" type="password" value={newCluster.github_token} onChange={e => setNewCluster({ ...newCluster, github_token: e.target.value })} placeholder="ghp_..." className="col-span-3" />
                                </div>
                                {createError && (
                                    <div className="col-span-4 text-sm text-red-500 bg-red-50 dark:bg-red-950 p-3 rounded">{createError}</div>
                                )}
                            </div>
                        ) : (
                            <div className="space-y-4 py-4">
                                <div className="p-4 bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-300 rounded-md border border-green-200 dark:border-green-800">
                                    <h4 className="font-semibold flex items-center gap-2">
                                        <Check className="h-4 w-4" /> Cluster Connected
                                    </h4>
                                    <p className="text-sm mt-1">Your infrastructure endpoints have been registered. Save this cluster token for API access:</p>
                                </div>
                                <div className="relative">
                                    <pre className="bg-slate-950 text-slate-50 p-4 rounded-lg text-sm font-mono overflow-x-auto">
                                        {createdClusterToken}
                                    </pre>
                                    <Button size="icon" variant="ghost" className="absolute top-2 right-2 hover:bg-slate-800 text-slate-400" onClick={copyToken}>
                                        {copied ? <Check className="h-4 w-4 text-green-500" /> : <Copy className="h-4 w-4" />}
                                    </Button>
                                </div>
                                <p className="text-xs text-muted-foreground text-center">
                                    This token will not be shown again.
                                </p>
                            </div>
                        )}

                        <DialogFooter>
                            {!createdClusterToken ? (
                                <Button onClick={handleCreateCluster} disabled={!newCluster.name}>Create Cluster</Button>
                            ) : (
                                <Button onClick={resetModal}>Done</Button>
                            )}
                        </DialogFooter>
                    </DialogContent>
                </Dialog>
            </div>

            {error && (
                <div className="p-4 bg-red-50 dark:bg-red-950 text-red-600 dark:text-red-400 rounded-lg border border-red-200 dark:border-red-800">
                    {error}
                </div>
            )}

            {loading ? (
                <div className="flex justify-center py-12">
                    <Loader2 className="h-8 w-8 animate-spin text-gray-400" />
                </div>
            ) : (
                <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
                    {clusters.map((cluster) => (
                        <Card key={cluster.id} className={cluster.status === "online" ? "border-green-500/50" : "border-zinc-200 dark:border-zinc-800"}>
                            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                                <CardTitle className="text-sm font-medium">{cluster.name}</CardTitle>
                                <Badge className={statusColor(cluster.status)}>
                                    {cluster.status === "online" ? "Online" : cluster.status === "maintenance" ? "Maintenance" : "Offline"}
                                </Badge>
                            </CardHeader>
                            <CardContent>
                                <div className="flex items-center gap-2 mt-1">
                                    <Server className={`h-5 w-5 ${cluster.status === "online" ? "text-green-500" : "text-gray-400"}`} />
                                    <span className="text-sm text-muted-foreground font-mono">{cluster.id.substring(0, 8)}...</span>
                                </div>
                                <div className="mt-3 text-xs text-muted-foreground space-y-1">
                                    {cluster.prometheus_url && <p>Prometheus: {cluster.prometheus_url}</p>}
                                    {cluster.github_repo && <p>GitHub: {cluster.github_repo}</p>}
                                    {!cluster.prometheus_url && !cluster.github_repo && <p>No infrastructure endpoints configured</p>}
                                </div>
                            </CardContent>
                            <CardFooter className="flex gap-2">
                                <Link href={`/clusters/${cluster.id}`} className="flex-1">
                                    <Button variant="outline" className="w-full">View Details</Button>
                                </Link>
                                <Button variant="ghost" size="icon" className="text-red-500 hover:text-red-600 hover:bg-red-50 dark:hover:bg-red-950" onClick={() => handleDeleteCluster(cluster.id, cluster.name)}>
                                    <Trash2 className="h-4 w-4" />
                                </Button>
                            </CardFooter>
                        </Card>
                    ))}

                    {clusters.length === 0 && !error && (
                        <div className="col-span-full text-center p-10 text-gray-500 border-2 border-dashed rounded-lg">
                            No clusters found. Click &quot;Connect Cluster&quot; to get started.
                        </div>
                    )}
                </div>
            )}
        </div>
    )
}
