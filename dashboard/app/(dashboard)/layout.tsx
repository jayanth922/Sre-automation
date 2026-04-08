"use client"

import Link from "next/link"
import { usePathname } from "next/navigation"
import { LayoutDashboard, Server, Settings, LogOut } from "lucide-react"
import { Button } from "@/components/ui/button"
import { useAuth } from "@/lib/auth-context"

export default function DashboardLayout({
    children,
}: {
    children: React.ReactNode
}) {
    const pathname = usePathname()
    const { user, logout } = useAuth()

    if (pathname.startsWith("/clusters/")) {
        return <>{children}</>
    }

    const pageTitle = (() => {
        if (pathname === "/") return "Overview"
        if (pathname.startsWith("/clusters/")) return "Cluster Dashboard"
        if (pathname.startsWith("/settings")) return "Settings"
        return pathname.split("/").filter(Boolean).pop() || "Dashboard"
    })()

    const navItems = [
        { name: "Overview", href: "/", icon: LayoutDashboard },
        { name: "Clusters", href: "/", icon: Server }, // For now same as home
        { name: "Settings", href: "/settings", icon: Settings },
    ]

    return (
        <div className="flex h-screen bg-gray-100 dark:bg-gray-900">
            {/* Sidebar */}
            <div className="w-64 bg-white dark:bg-gray-800 border-r border-gray-200 dark:border-gray-700 flex flex-col">
                <div className="p-6">
                    <h1 className="text-xl font-bold text-gray-800 dark:text-white">SRE Platform</h1>
                </div>
                <nav className="flex-1 px-4 space-y-2">
                    {navItems.map((item) => {
                        const Icon = item.icon
                        const isActive = pathname === item.href
                        return (
                            <Link
                                key={item.name}
                                href={item.href}
                                className={`flex items-center gap-3 px-4 py-2 rounded-md text-sm font-medium transition-colors ${isActive
                                        ? "bg-primary text-primary-foreground"
                                        : "text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700"
                                    }`}
                            >
                                <Icon size={18} />
                                {item.name}
                            </Link>
                        )
                    })}
                </nav>
                <div className="p-4 border-t border-gray-200 dark:border-gray-700">
                    <Button variant="ghost" className="w-full justify-start gap-3" onClick={logout}>
                        <LogOut size={18} />
                        Logout
                    </Button>
                </div>
            </div>

            {/* Main Content */}
            <div className="flex-1 overflow-auto">
                <header className="bg-white dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700 p-4">
                    <div className="flex justify-between items-center">
                        <h2 className="text-lg font-semibold capitalize">
                            {pageTitle}
                        </h2>
                        <div className="flex items-center gap-4">
                            <span className="text-sm text-gray-500">{user?.email || "Unknown"}</span>
                            {/* <Avatar /> can go here */}
                        </div>
                    </div>
                </header>
                <main className="p-6">
                    {children}
                </main>
            </div>
        </div>
    )
}
