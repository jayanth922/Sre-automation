"use client"

import { useEffect, useRef, useState } from "react"
import { Building2, ChevronDown, KeyRound, LogOut, Mail, Settings, ShieldUser, UserCircle2 } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { api, useAuth } from "@/lib/auth-context"

interface UserProfile {
    id: string
    email: string
    full_name: string | null
    display_name: string
    role: string
    org_id: string
    organization_name: string
    is_active: boolean
    created_at: string
}

export function UserAccountMenu() {
    const { user, logout } = useAuth()
    const menuRef = useRef<HTMLDivElement | null>(null)
    const [menuOpen, setMenuOpen] = useState(false)
    const [settingsOpen, setSettingsOpen] = useState(false)
    const [loadingProfile, setLoadingProfile] = useState(false)
    const [profile, setProfile] = useState<UserProfile | null>(null)
    const [passwordCurrent, setPasswordCurrent] = useState("")
    const [passwordNext, setPasswordNext] = useState("")
    const [passwordConfirm, setPasswordConfirm] = useState("")
    const [passwordSaving, setPasswordSaving] = useState(false)
    const [passwordMessage, setPasswordMessage] = useState<string | null>(null)
    const [passwordError, setPasswordError] = useState<string | null>(null)

    const displayName = profile?.display_name || user?.email || "User"

    useEffect(() => {
        let active = true

        const loadProfile = async () => {
            setLoadingProfile(true)
            try {
                const response = await api.get("/auth/me")
                if (!active) return
                setProfile(response.data as UserProfile)
            } catch {
                if (active) {
                    setProfile(null)
                }
            } finally {
                if (active) {
                    setLoadingProfile(false)
                }
            }
        }

        void loadProfile()

        return () => {
            active = false
        }
    }, [])

    useEffect(() => {
        const handlePointerDown = (event: MouseEvent) => {
            if (!menuRef.current?.contains(event.target as Node)) {
                setMenuOpen(false)
            }
        }

        document.addEventListener("mousedown", handlePointerDown)
        return () => document.removeEventListener("mousedown", handlePointerDown)
    }, [])

    useEffect(() => {
        if (!settingsOpen) {
            setPasswordCurrent("")
            setPasswordNext("")
            setPasswordConfirm("")
            setPasswordError(null)
            setPasswordMessage(null)
        }
    }, [settingsOpen])

    const submitPasswordReset = async () => {
        setPasswordError(null)
        setPasswordMessage(null)

        if (!passwordCurrent || !passwordNext) {
            setPasswordError("Enter your current password and a new password.")
            return
        }

        if (passwordNext.length < 8) {
            setPasswordError("New password must be at least 8 characters long.")
            return
        }

        if (passwordNext !== passwordConfirm) {
            setPasswordError("New password and confirmation do not match.")
            return
        }

        setPasswordSaving(true)
        try {
            await api.post("/auth/password", {
                current_password: passwordCurrent,
                new_password: passwordNext,
            })
            setPasswordMessage("Password updated successfully.")
            setPasswordCurrent("")
            setPasswordNext("")
            setPasswordConfirm("")
        } catch (error: any) {
            setPasswordError(error?.response?.data?.detail || "Failed to update password.")
        } finally {
            setPasswordSaving(false)
        }
    }

    const accountInitials = (profile?.display_name || displayName)
        .split(" ")
        .filter(Boolean)
        .slice(0, 2)
        .map((part) => part[0]?.toUpperCase() || "")
        .join("") || "U"

    return (
        <div ref={menuRef} className="relative">
            <button
                type="button"
                onClick={() => setMenuOpen((current) => !current)}
                className="flex items-center gap-3 rounded-full border border-zinc-800 bg-zinc-950/70 px-3 py-2 text-left text-sm text-zinc-100 transition hover:border-cyan-500/30 hover:bg-zinc-900"
            >
                <span className="flex h-9 w-9 items-center justify-center rounded-full border border-cyan-500/20 bg-cyan-500/10 text-sm font-semibold text-cyan-200">
                    {accountInitials}
                </span>
                <span className="min-w-0">
                    <span className="block max-w-[220px] truncate font-medium text-white">
                        {loadingProfile ? "Loading profile..." : displayName}
                    </span>
                    <span className="block max-w-[220px] truncate text-xs text-zinc-500">
                        {profile?.organization_name || user?.org_id || "Organization"}
                    </span>
                </span>
                <ChevronDown className="h-4 w-4 text-zinc-400" />
            </button>

            {menuOpen && (
                <div className="absolute right-0 z-40 mt-3 w-[320px] overflow-hidden rounded-3xl border border-zinc-800 bg-zinc-950/95 shadow-2xl shadow-black/40 backdrop-blur">
                    <div className="border-b border-zinc-800 px-4 py-4">
                        <div className="flex items-center gap-3">
                            <div className="flex h-11 w-11 items-center justify-center rounded-2xl border border-cyan-500/20 bg-cyan-500/10 text-cyan-200">
                                <UserCircle2 className="h-5 w-5" />
                            </div>
                            <div className="min-w-0">
                                <p className="truncate text-sm font-medium text-white">{displayName}</p>
                                <p className="truncate text-xs text-zinc-500">{profile?.email || user?.email || "Signed in user"}</p>
                            </div>
                        </div>
                    </div>

                    <div className="space-y-1 p-2">
                        <div className="grid gap-2 rounded-2xl bg-zinc-900/60 p-3 text-xs text-zinc-300">
                            <div className="flex items-center gap-2">
                                <Mail className="h-3.5 w-3.5 text-cyan-300" />
                                <span className="truncate">{profile?.email || user?.email || "Unknown email"}</span>
                            </div>
                            <div className="flex items-center gap-2">
                                <Building2 className="h-3.5 w-3.5 text-cyan-300" />
                                <span className="truncate">{profile?.organization_name || user?.org_id || "Unknown organization"}</span>
                            </div>
                            <div className="flex items-center gap-2">
                                <ShieldUser className="h-3.5 w-3.5 text-cyan-300" />
                                <span className="capitalize">{profile?.role || user?.role || "member"}</span>
                            </div>
                        </div>

                        <button
                            type="button"
                            onClick={() => {
                                setSettingsOpen(true)
                                setMenuOpen(false)
                            }}
                            className="flex w-full items-center gap-3 rounded-2xl px-3 py-2 text-sm text-zinc-200 transition hover:bg-zinc-900"
                        >
                            <Settings className="h-4 w-4 text-zinc-400" />
                            Settings
                        </button>

                        <button
                            type="button"
                            onClick={logout}
                            className="flex w-full items-center gap-3 rounded-2xl px-3 py-2 text-sm text-rose-200 transition hover:bg-rose-500/10"
                        >
                            <LogOut className="h-4 w-4" />
                            Logout
                        </button>
                    </div>
                </div>
            )}

            <Dialog open={settingsOpen} onOpenChange={setSettingsOpen}>
                <DialogContent className="max-w-3xl border-zinc-800 bg-zinc-950 text-zinc-100">
                    <DialogHeader>
                        <DialogTitle className="text-white">Account Settings</DialogTitle>
                        <DialogDescription className="text-zinc-400">
                            Review your account details and update your password from one place.
                        </DialogDescription>
                    </DialogHeader>

                    <div className="grid gap-4 md:grid-cols-2">
                        <div className="space-y-4 rounded-3xl border border-zinc-800 bg-zinc-900/40 p-4">
                            <div className="flex items-center gap-2 text-xs uppercase tracking-[0.24em] text-zinc-500">
                                <UserCircle2 className="h-3.5 w-3.5 text-cyan-300" />
                                Profile
                            </div>
                            <div className="space-y-3 text-sm">
                                <div>
                                    <p className="text-xs uppercase tracking-[0.24em] text-zinc-500">Display name</p>
                                    <p className="mt-1 text-zinc-100">{profile?.display_name || user?.email || "Unknown"}</p>
                                </div>
                                <div>
                                    <p className="text-xs uppercase tracking-[0.24em] text-zinc-500">Email</p>
                                    <p className="mt-1 break-all text-zinc-300">{profile?.email || user?.email || "Unknown"}</p>
                                </div>
                                <div>
                                    <p className="text-xs uppercase tracking-[0.24em] text-zinc-500">User ID</p>
                                    <p className="mt-1 break-all font-mono text-xs text-zinc-400">{profile?.id || user?.user_id || "Unavailable"}</p>
                                </div>
                                <div>
                                    <p className="text-xs uppercase tracking-[0.24em] text-zinc-500">Role</p>
                                    <p className="mt-1 capitalize text-zinc-100">{profile?.role || user?.role || "member"}</p>
                                </div>
                            </div>
                        </div>

                        <div className="space-y-4 rounded-3xl border border-zinc-800 bg-zinc-900/40 p-4">
                            <div className="flex items-center gap-2 text-xs uppercase tracking-[0.24em] text-zinc-500">
                                <Building2 className="h-3.5 w-3.5 text-cyan-300" />
                                Organization
                            </div>
                            <div className="space-y-3 text-sm">
                                <div>
                                    <p className="text-xs uppercase tracking-[0.24em] text-zinc-500">Organization name</p>
                                    <p className="mt-1 text-zinc-100">{profile?.organization_name || "Unknown organization"}</p>
                                </div>
                                <div>
                                    <p className="text-xs uppercase tracking-[0.24em] text-zinc-500">Organization ID</p>
                                    <p className="mt-1 break-all font-mono text-xs text-zinc-400">{profile?.org_id || user?.org_id || "Unavailable"}</p>
                                </div>
                                <div>
                                    <p className="text-xs uppercase tracking-[0.24em] text-zinc-500">Account status</p>
                                    <p className="mt-1 text-zinc-100">{profile?.is_active ? "Active" : "Inactive"}</p>
                                </div>
                                <div>
                                    <p className="text-xs uppercase tracking-[0.24em] text-zinc-500">Created at</p>
                                    <p className="mt-1 text-zinc-300">{profile?.created_at ? new Date(profile.created_at).toLocaleString() : "Unavailable"}</p>
                                </div>
                            </div>
                        </div>
                    </div>

                    <div className="rounded-3xl border border-zinc-800 bg-zinc-900/40 p-4">
                        <div className="mb-4 flex items-center gap-2 text-xs uppercase tracking-[0.24em] text-zinc-500">
                            <KeyRound className="h-3.5 w-3.5 text-cyan-300" />
                            Password reset
                        </div>
                        <div className="grid gap-4 md:grid-cols-3">
                            <div className="space-y-2">
                                <Label htmlFor="current-password" className="text-zinc-300">Current password</Label>
                                <Input
                                    id="current-password"
                                    type="password"
                                    value={passwordCurrent}
                                    onChange={(event) => setPasswordCurrent(event.target.value)}
                                    className="border-zinc-800 bg-zinc-950 text-zinc-100"
                                />
                            </div>
                            <div className="space-y-2">
                                <Label htmlFor="new-password" className="text-zinc-300">New password</Label>
                                <Input
                                    id="new-password"
                                    type="password"
                                    value={passwordNext}
                                    onChange={(event) => setPasswordNext(event.target.value)}
                                    className="border-zinc-800 bg-zinc-950 text-zinc-100"
                                />
                            </div>
                            <div className="space-y-2">
                                <Label htmlFor="confirm-password" className="text-zinc-300">Confirm password</Label>
                                <Input
                                    id="confirm-password"
                                    type="password"
                                    value={passwordConfirm}
                                    onChange={(event) => setPasswordConfirm(event.target.value)}
                                    className="border-zinc-800 bg-zinc-950 text-zinc-100"
                                />
                            </div>
                        </div>

                        {passwordError && (
                            <div className="mt-4 rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-200">
                                {passwordError}
                            </div>
                        )}

                        {passwordMessage && (
                            <div className="mt-4 rounded-2xl border border-emerald-500/30 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-200">
                                {passwordMessage}
                            </div>
                        )}
                    </div>

                    <DialogFooter>
                        <Button
                            variant="outline"
                            onClick={() => setSettingsOpen(false)}
                            className="border-zinc-800 bg-zinc-950 text-zinc-200 hover:bg-zinc-900"
                        >
                            Close
                        </Button>
                        <Button
                            onClick={() => void submitPasswordReset()}
                            disabled={passwordSaving}
                            className="gap-2 bg-cyan-500 text-slate-950 hover:bg-cyan-400"
                        >
                            <KeyRound className="h-4 w-4" />
                            {passwordSaving ? "Saving..." : "Reset Password"}
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>
        </div>
    )
}