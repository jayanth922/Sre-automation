"use client"

import { usePathname } from "next/navigation"
import { UserAccountMenu } from "@/components/dashboard/UserAccountMenu"

export default function DashboardLayout({
    children,
}: {
    children: React.ReactNode
}) {
    const pathname = usePathname()
    const isIncidentDashboard = pathname.includes("/incidents/") && pathname.split("/").filter(Boolean).length === 4

    const pageTitle = (() => {
        if (pathname === "/") return "Clusters"
        if (pathname.includes("/incidents/") && pathname.split("/").filter(Boolean).length === 4) return "Incident Dashboard"
        if (pathname.endsWith("/incidents")) return "Incidents"
        if (pathname.includes("/audit")) return "Audit Trail"
        return pathname.split("/").filter(Boolean).pop() || "Dashboard"
    })()

    return (
        <div className="flex h-screen flex-col overflow-hidden bg-[radial-gradient(circle_at_top_left,_rgba(34,211,238,0.10),_transparent_28%),linear-gradient(180deg,_#090b14_0%,_#05070f_100%)] text-zinc-50">
            <header className="sticky top-0 z-30 border-b border-zinc-800/70 bg-zinc-950/80 backdrop-blur">
                <div className="flex w-full items-center justify-between gap-4 px-4 py-4 md:px-6">
                    <div className="min-w-0">
                        <p className="text-[11px] uppercase tracking-[0.28em] text-zinc-500">SRE platform</p>
                        <h2 className="truncate text-xl font-semibold tracking-tight text-white md:text-2xl">
                            {pageTitle}
                        </h2>
                    </div>
                    <UserAccountMenu />
                </div>
            </header>
            <main className={`flex w-full flex-1 min-h-0 flex-col ${isIncidentDashboard ? "overflow-hidden px-4 py-4 md:px-6" : "overflow-auto overscroll-none px-4 py-6 md:px-6"}`}>
                {children}
            </main>
        </div>
    )
}
