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

// The raw token comes back exactly once, on creation. Nothing can reissue it.
interface Invitation {
  id: string
  email: string
  role: "admin" | "member"
  expires_at: string
  token: string
}

export default function TeamPage() {
  const { user } = useAuth()
  const isAdmin = (user?.role ?? "member") === "admin"
  const meId = user?.user_id ?? ""

  const [members, setMembers] = useState<Member[] | null>(null)
  const [org, setOrg] = useState<Org | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [slackToken, setSlackToken] = useState("")
  const [slackSaving, setSlackSaving] = useState(false)
  const [slackErr, setSlackErr] = useState<string | null>(null)
  const [langfusePublicKey, setLangfusePublicKey] = useState("")
  const [langfuseSecretKey, setLangfuseSecretKey] = useState("")
  const [langfuseHost, setLangfuseHost] = useState("")
  const [langfuseSaving, setLangfuseSaving] = useState(false)
  const [langfuseErr, setLangfuseErr] = useState<string | null>(null)
  const [inviteEmail, setInviteEmail] = useState("")
  const [inviteRole, setInviteRole] = useState<"admin" | "member">("member")
  const [inviteHours, setInviteHours] = useState("72")
  const [inviteSaving, setInviteSaving] = useState(false)
  const [inviteErr, setInviteErr] = useState<string | null>(null)
  const [invite, setInvite] = useState<Invitation | null>(null)
  const [copied, setCopied] = useState(false)
  const [origin, setOrigin] = useState("")

  const orgId = user?.org_id ?? ""
  const inviteLink = invite
    ? `${origin}/accept-invite?token=${encodeURIComponent(invite.token)}`
    : ""

  useEffect(() => {
    setOrigin(window.location.origin)
  }, [])

  const createInvitation = async () => {
    if (!orgId) return
    setInviteSaving(true)
    setInviteErr(null)
    setCopied(false)
    try {
      const { data } = await api.post<Invitation>(`/organizations/${orgId}/invitations`, {
        email: inviteEmail.trim(),
        role: inviteRole,
        expires_in_hours: Number(inviteHours),
      })
      setInvite(data)
      setInviteEmail("")
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
      setInviteErr(
        typeof detail === "string"
          ? detail
          : Array.isArray(detail)
            ? "That doesn't look like a valid email address."
            : "Could not create this invitation.",
      )
    } finally {
      setInviteSaving(false)
    }
  }

  const copyInvite = async () => {
    try {
      await navigator.clipboard.writeText(inviteLink)
      setCopied(true)
    } catch {
      // Clipboard access needs a secure context. The field is selectable.
      setInviteErr("Could not reach the clipboard — select the link and copy it by hand.")
    }
  }

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
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 10, marginBottom: 8 }}>
          <input
            className="sx-input"
            type="password"
            placeholder="xoxb-... bot token"
            value={slackToken}
            onChange={(e) => setSlackToken(e.target.value)}
            disabled={!isAdmin || slackSaving}
            style={{ flex: 1, maxWidth: 360 }}
          />
          <button
            className="sx-btn"
            style={{ flex: "none", padding: "6px 12px", fontSize: 12 }}
            disabled={!isAdmin || slackSaving || !slackToken.trim()}
            onClick={async () => {
              setSlackSaving(true)
              setSlackErr(null)
              try {
                const { data } = await api.post<Org>("/organization/slack/bot-token", {
                  bot_token: slackToken.trim(),
                })
                setOrg(data)
                setSlackToken("")
              } catch (e) {
                const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
                setSlackErr(detail || "Could not save this Slack bot token.")
              } finally {
                setSlackSaving(false)
              }
            }}
          >
            {slackSaving ? "Saving…" : org?.slack_team_id ? "Update token" : "Connect"}
          </button>
          {org?.slack_team_id ? (
            <span className="sx-badge ok">connected · {org.slack_team_id}</span>
          ) : (
            !isAdmin && <span className="sx-mono" style={{ fontSize: 11, color: "var(--ink3)" }}>only admins can connect Slack</span>
          )}
        </div>
        <div className="sx-mono" style={{ fontSize: 11, color: "var(--ink3)", marginBottom: 18 }}>
          Paste a Bot User OAuth Token from your Slack app&apos;s &quot;OAuth &amp; Permissions&quot; page
          (Install to Workspace). Verified against Slack before saving.
        </div>
        {slackErr && <ErrorNote>{slackErr}</ErrorNote>}

        <SectionTitle title="Langfuse" meta="LLM tracing for your organization's agent runs" />
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 10, marginTop: 10, marginBottom: 8 }}>
          <input
            className="sx-input"
            type="text"
            placeholder="pk-lf-... public key"
            value={langfusePublicKey}
            onChange={(e) => setLangfusePublicKey(e.target.value)}
            disabled={!isAdmin || langfuseSaving}
            style={{ flex: 1, minWidth: 180, maxWidth: 260 }}
          />
          <input
            className="sx-input"
            type="password"
            placeholder="sk-lf-... secret key"
            value={langfuseSecretKey}
            onChange={(e) => setLangfuseSecretKey(e.target.value)}
            disabled={!isAdmin || langfuseSaving}
            style={{ flex: 1, minWidth: 180, maxWidth: 260 }}
          />
          <input
            className="sx-input"
            type="text"
            placeholder="host (optional, defaults to cloud.langfuse.com)"
            value={langfuseHost}
            onChange={(e) => setLangfuseHost(e.target.value)}
            disabled={!isAdmin || langfuseSaving}
            style={{ flex: 1, minWidth: 200, maxWidth: 300 }}
          />
          <button
            className="sx-btn"
            style={{ flex: "none", padding: "6px 12px", fontSize: 12 }}
            disabled={!isAdmin || langfuseSaving || !langfusePublicKey.trim() || !langfuseSecretKey.trim()}
            onClick={async () => {
              setLangfuseSaving(true)
              setLangfuseErr(null)
              try {
                const { data } = await api.post<Org>("/organization/langfuse", {
                  public_key: langfusePublicKey.trim(),
                  secret_key: langfuseSecretKey.trim(),
                  host: langfuseHost.trim() || null,
                })
                setOrg(data)
                setLangfusePublicKey("")
                setLangfuseSecretKey("")
                setLangfuseHost("")
              } catch (e) {
                const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
                setLangfuseErr(detail || "Could not save these Langfuse keys.")
              } finally {
                setLangfuseSaving(false)
              }
            }}
          >
            {langfuseSaving ? "Saving…" : org?.langfuse_public_key ? "Update keys" : "Connect"}
          </button>
          {org?.langfuse_public_key ? (
            <span className="sx-badge ok">connected · {org.langfuse_public_key}</span>
          ) : (
            !isAdmin && <span className="sx-mono" style={{ fontSize: 11, color: "var(--ink3)" }}>only admins can connect Langfuse</span>
          )}
        </div>
        <div className="sx-mono" style={{ fontSize: 11, color: "var(--ink3)", marginBottom: 18 }}>
          Paste your Langfuse project&apos;s public and secret keys (Settings → API Keys in Langfuse).
          Each organization traces to its own project — until this is set, this organization&apos;s
          agent runs simply go untraced.
        </div>
        {langfuseErr && <ErrorNote>{langfuseErr}</ErrorNote>}

        <SectionTitle
          title="Invitations"
          meta={isAdmin ? "the only way to add someone to this organization" : "admins invite new teammates"}
        />

        {isAdmin ? (
          <>
            <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 10, marginTop: 10, marginBottom: 8 }}>
              <input
                className="sx-input"
                type="email"
                placeholder="teammate@example.com"
                value={inviteEmail}
                onChange={(e) => setInviteEmail(e.target.value)}
                disabled={inviteSaving}
                style={{ flex: 1, minWidth: 200, maxWidth: 300 }}
                aria-label="Email address to invite"
              />
              <select
                className="sx-input"
                style={{ width: 118, padding: "5px 8px", fontSize: 12 }}
                value={inviteRole}
                disabled={inviteSaving}
                onChange={(e) => setInviteRole(e.target.value as "admin" | "member")}
                aria-label="Role for the invited teammate"
              >
                <option value="member">Member</option>
                <option value="admin">Admin</option>
              </select>
              <select
                className="sx-input"
                style={{ width: 138, padding: "5px 8px", fontSize: 12 }}
                value={inviteHours}
                disabled={inviteSaving}
                onChange={(e) => setInviteHours(e.target.value)}
                aria-label="How long the invitation stays valid"
              >
                <option value="24">Valid 24 hours</option>
                <option value="72">Valid 3 days</option>
                <option value="168">Valid 7 days</option>
                <option value="720">Valid 30 days</option>
              </select>
              <button
                className="sx-btn"
                style={{ flex: "none", padding: "6px 12px", fontSize: 12 }}
                disabled={inviteSaving || !inviteEmail.trim() || !orgId}
                onClick={createInvitation}
              >
                {inviteSaving ? "Creating…" : "Create invitation"}
              </button>
            </div>
            <div className="sx-mono" style={{ fontSize: 11, color: "var(--ink3)", marginBottom: invite ? 10 : 18 }}>
              Nothing is emailed — you get a single-use link to pass on yourself. Inviting the same
              address again revokes the earlier link.
            </div>
            {inviteErr && <ErrorNote>{inviteErr}</ErrorNote>}
            {invite && (
              <div className="sx-empty" style={{ textAlign: "left", padding: 14, marginBottom: 18 }}>
                <div style={{ fontWeight: 500, marginBottom: 6 }}>
                  Invitation for {invite.email} · {invite.role === "admin" ? "Admin" : "Member"}
                </div>
                <div className="sx-mono" style={{ fontSize: 11, color: "var(--ink3)", marginBottom: 8 }}>
                  Shown once — it cannot be retrieved later. Expires{" "}
                  {new Date(invite.expires_at).toLocaleString()}.
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                  <input
                    className="sx-input sx-mono"
                    readOnly
                    value={inviteLink}
                    onFocus={(e) => e.currentTarget.select()}
                    style={{ flex: 1, minWidth: 240, fontSize: 11 }}
                    aria-label="Invitation link"
                  />
                  <button
                    className="sx-btn"
                    style={{ flex: "none", padding: "6px 12px", fontSize: 12 }}
                    onClick={copyInvite}
                  >
                    {copied ? "Copied" : "Copy link"}
                  </button>
                  <button
                    className="sx-btn"
                    style={{ flex: "none", padding: "6px 12px", fontSize: 12 }}
                    onClick={() => {
                      setInvite(null)
                      setCopied(false)
                    }}
                  >
                    Dismiss
                  </button>
                </div>
              </div>
            )}
          </>
        ) : (
          <p style={{ color: "var(--ink2)", fontSize: 12.5, margin: "10px 0 18px", lineHeight: 1.6 }}>
            Only admins can invite new teammates.
          </p>
        )}

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
                            aria-label={`Change role for ${m.full_name?.trim() || m.email}`}
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
              Open registration closed when this installation was claimed, so an invitation is the
              only way in — someone registering with your organization&apos;s name would create a
              second, separate organization instead of joining yours. Roles can be changed here
              afterwards. The last active admin can&apos;t be demoted or deactivated, so your
              organization can never lock itself out.
            </>
          ) : (
            <>Only admins can assign roles or manage access. Ask an admin if you need a role change.</>
          )}
        </p>
      </div>
    </ConsolePage>
  )
}
