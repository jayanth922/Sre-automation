"use client"

import { useCallback, useEffect, useState } from "react"
import { api, useAuth } from "@/lib/auth-context"
import { ConsolePage } from "@/components/console/ConsolePage"
import { SectionTitle, Spinner, Empty, ErrorNote } from "@/components/console/ui"
import { timeAgo, type Org } from "@/lib/console"

interface Member {
  id: string
  email: string
  full_name: string | null
  role: "admin" | "member"
  is_active: boolean
  created_at: string
}

export default function TeamPage() {
  const { user } = useAuth()
  const isAdmin = (user?.role ?? "member") === "admin"
  const meId = user?.user_id ?? ""

  const [members, setMembers] = useState<Member[] | null>(null)
  const [org, setOrg] = useState<Org | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [slackConnecting, setSlackConnecting] = useState(false)
  const [slackErr, setSlackErr] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const r = await api.get<Member[]>("/organization/members")
      setMembers(r.data)
      setErr(null)
    } catch {
      setErr("Could not load your team.")
    }
  }, [])

  const loadOrg = useCallback(async () => {
    try {
      const r = await api.get<Org>("/organization")
      setOrg(r.data)
    } catch {
      /* org info is a nice-to-have; Slack section falls back to no status */
    }
  }, [])

  useEffect(() => {
    load()
    loadOrg()
  }, [load, loadOrg])

  const mutate = async (m: Member, action: () => Promise<unknown>) => {
    setBusy(m.id)
    setErr(null)
    try {
      await action()
      await load()
    } catch (e) {
      const ax = e as { response?: { data?: { detail?: string } } }
      setErr(ax.response?.data?.detail ?? "Could not apply the change.")
    } finally {
      setBusy(null)
    }
  }

  const setRole = (m: Member, role: "admin" | "member") =>
    mutate(m, () => api.patch(`/organization/members/${m.id}/role`, { role }))

  const setActive = (m: Member, is_active: boolean) =>
    mutate(m, () => api.patch(`/organization/members/${m.id}/status`, { is_active }))

  return (
    <ConsolePage title="Team">
      <div style={{ maxWidth: 820 }}>
        <SectionTitle title="Slack" meta="incident notifications for your organization" />
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 10, marginBottom: 26 }}>
          <button
            className="sx-btn"
            style={{ flex: "none", padding: "6px 12px", fontSize: 12 }}
            disabled={!isAdmin || slackConnecting}
            onClick={async () => {
              setSlackConnecting(true)
              setSlackErr(null)
              try {
                const { data } = await api.get<{ install_url: string }>("/organizations/slack/install-url")
                window.location.href = data.install_url
              } catch {
                setSlackErr("Could not start the Slack install flow.")
                setSlackConnecting(false)
              }
            }}
          >
            {slackConnecting ? "Redirecting…" : org?.slack_team_id ? "Reconnect Slack" : "Add to Slack"}
          </button>
          {org?.slack_team_id ? (
            <span className="sx-badge ok">connected · {org.slack_team_id}</span>
          ) : (
            !isAdmin && <span className="sx-mono" style={{ fontSize: 11, color: "var(--ink3)" }}>only admins can connect Slack</span>
          )}
        </div>
        {slackErr && <ErrorNote>{slackErr}</ErrorNote>}

        <SectionTitle
          title="Members"
          meta={isAdmin ? "assign roles and manage access" : "everyone in your organization"}
        />

        {err && <ErrorNote>{err}</ErrorNote>}

        {members === null ? (
          <div style={{ padding: 40, display: "flex", justifyContent: "center" }}>
            <Spinner />
          </div>
        ) : members.length === 0 ? (
          <Empty>No members yet.</Empty>
        ) : (
          <table className="sx-tbl">
            <thead>
              <tr>
                <th className="l">Member</th>
                <th className="l">Role</th>
                <th className="l">Status</th>
                <th>Joined</th>
                {isAdmin && <th className="l">Actions</th>}
              </tr>
            </thead>
            <tbody>
              {members.map((m) => {
                const isMe = m.id === meId
                const rowBusy = busy === m.id
                return (
                  <tr key={m.id}>
                    <td className="l">
                      <div style={{ fontWeight: 500 }}>
                        {m.full_name?.trim() || m.email}
                        {isMe && <span style={{ color: "var(--ink3)", fontWeight: 400 }}> · you</span>}
                      </div>
                      {m.full_name?.trim() && (
                        <div className="sx-mono" style={{ fontSize: 11, color: "var(--ink3)" }}>{m.email}</div>
                      )}
                    </td>
                    <td className="l">
                      <span className={`sx-badge ${m.role === "admin" ? "sel" : "neutral"}`}>
                        {m.role === "admin" ? "Admin" : "Member"}
                      </span>
                    </td>
                    <td className="l">
                      <span className={`sx-badge ${m.is_active ? "ok" : "crit"}`}>
                        {m.is_active ? "Active" : "Deactivated"}
                      </span>
                    </td>
                    <td>{timeAgo(m.created_at)}</td>
                    {isAdmin && (
                      <td className="l">
                        <div style={{ display: "flex", gap: 8, alignItems: "center", justifyContent: "flex-start" }}>
                          <select
                            className="sx-input"
                            style={{ width: 118, padding: "5px 8px", fontSize: 12 }}
                            value={m.role}
                            disabled={rowBusy || isMe}
                            onChange={(e) => setRole(m, e.target.value as "admin" | "member")}
                            title={isMe ? "You can't change your own role" : "Change role"}
                          >
                            <option value="admin">Admin</option>
                            <option value="member">Member</option>
                          </select>
                          <button
                            className="sx-btn"
                            style={{ flex: "none", padding: "6px 10px", fontSize: 11.5 }}
                            disabled={rowBusy || isMe}
                            onClick={() => setActive(m, !m.is_active)}
                            title={isMe ? "You can't deactivate yourself" : m.is_active ? "Revoke access" : "Restore access"}
                          >
                            {m.is_active ? "Deactivate" : "Reactivate"}
                          </button>
                        </div>
                      </td>
                    )}
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}

        <p style={{ color: "var(--ink2)", fontSize: 12.5, marginTop: 22, lineHeight: 1.6, maxWidth: 620 }}>
          {isAdmin ? (
            <>
              New teammates join by registering with your organization name. They start as members —
              promote anyone to admin here. The last active admin can&apos;t be demoted or deactivated,
              so your organization can never lock itself out.
            </>
          ) : (
            <>Only admins can assign roles or manage access. Ask an admin if you need a role change.</>
          )}
        </p>
      </div>
    </ConsolePage>
  )
}
