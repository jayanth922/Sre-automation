#!/usr/bin/env python3
"""A mounted router with no caller is a feature nobody can reach.

The jobs and tickets routers were both mounted, both authenticated, and both
invisible: nothing in the dashboard ever called them. Neither the type check
nor the production build can notice that, because an unreferenced endpoint is
not a type error. These tests pin the call sites instead.
"""

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_DASH = _ROOT / "dashboard"
_JOBS_PAGE = _DASH / "app" / "(dashboard)" / "clusters" / "[id]" / "jobs" / "page.tsx"
_INCIDENT_PAGE = (
    _DASH
    / "app"
    / "(dashboard)"
    / "clusters"
    / "[id]"
    / "incidents"
    / "[incidentId]"
    / "page.tsx"
)
_BREAK_GLASS = _DASH / "components" / "console" / "BreakGlass.tsx"
_HOME_PAGE = _DASH / "app" / "(dashboard)" / "page.tsx"
_SLOS_PAGE = _DASH / "app" / "(dashboard)" / "clusters" / "[id]" / "slos" / "page.tsx"
_INSIGHTS_PAGE = (
    _DASH / "app" / "(dashboard)" / "clusters" / "[id]" / "insights" / "page.tsx"
)
_TOASTS = _DASH / "components" / "console" / "IncidentToasts.tsx"
_CLUSTER_LAYOUT = _DASH / "app" / "(dashboard)" / "clusters" / "[id]" / "layout.tsx"
_SETTINGS_PAGE = (
    _DASH / "app" / "(dashboard)" / "clusters" / "[id]" / "settings" / "page.tsx"
)


def test_a_failed_cluster_list_is_not_rendered_as_an_empty_account():
    """"Could not ask" and "you have none" are different answers.

    The picker falls back to an empty array so it can leave its loading state,
    which meant an unreachable API rendered the first-run onboarding page and
    invited an operator with running clusters to connect another one.
    """
    src = _HOME_PAGE.read_text()
    assert "setLoadErr(true)" in src
    assert "if (loadErr) {" in src


def test_general_chat_is_not_an_unbounded_execution_surface():
    """Slack incident threads are the only user-facing conversation surface."""
    runtime = (_ROOT / "sre_agent" / "agent_runtime.py").read_text()

    assert "chat_router" not in runtime
    assert not (_ROOT / "sre_agent" / "api" / "v1" / "chat.py").exists()


def test_an_incident_message_cannot_queue_an_agent_turn_over_http():
    """The follow-up route is gone, and so are the two components that called it.

    `POST /incidents/{id}/message` spent a full agent turn for any org member
    holding an incident id, while its only callers sat in
    `components/dashboard/` -- a directory nothing imports. Reachable route, no
    reachable UI: the same shape as `POST /chat`.

    The conversational handler itself stays. Slack routes thread replies
    straight into it, and that is the one surface the product commits to.
    """
    mc = (_ROOT / "sre_agent" / "api" / "v1" / "mission_control.py").read_text()
    assert '"/{incident_id}/message"' not in mc
    assert "async def send_incident_message" not in mc
    assert "IncidentMessageRequest" not in mc
    assert "IncidentMessageRequest" not in (_ROOT / "backend" / "schemas.py").read_text()

    assert "async def handle_incident_message" in mc
    assert "handle_incident_message" in (_ROOT / "sre_agent" / "war_room.py").read_text()

    legacy = _DASH / "components" / "dashboard"
    assert not (legacy / "IncidentChatPanel.tsx").exists()
    assert not (legacy / "IncidentCommandCenter.tsx").exists()


def test_the_console_can_reach_the_emergency_lock():
    """Both lock endpoints have a caller.

    This is the pair a path-level audit found stranded. It is enforced for
    real -- `mutation_gateway.py` rejects every mutation with `cluster_locked`
    while it is set -- and Slack has no command for it, whose vocabulary is
    `approve fix`, `deny`, `acknowledge` and `mark resolved`. Until these call
    sites existed, pulling the break glass meant hand-rolling an authenticated
    HTTP request.
    """
    src = _BREAK_GLASS.read_text()
    assert "`/clusters/${clusterId}/lock`" in src
    assert "api.post(`/clusters/${clusterId}/lock`, { locked: next })" in src


def test_an_unknown_lock_state_is_never_rendered_as_released():
    """`locked === null` is a third state, and collapsing it is the bug.

    The backend read fails open -- `is_cluster_locked` returns False when Redis
    is down -- so "false" and "we could not ask" arrive looking identical
    unless `state_available` is consulted. The banner renders on `=== true`
    only, and the control refuses to offer a toggle it cannot ground.
    """
    src = _BREAK_GLASS.read_text()
    assert "data.state_available !== false" in src
    assert "if (locked !== true) return null" in src
    assert "disabled={!isAdmin || locked === null}" in src


def test_the_lock_is_visible_from_every_cluster_page():
    """A banner only on Settings would be a banner nobody sees in an incident."""
    layout = _CLUSTER_LAYOUT.read_text()
    assert "<LockProvider clusterId={id}>" in layout
    assert "<BreakGlassBanner />" in layout


def test_the_break_glass_does_not_sit_under_the_save_bar():
    """The settings save bar claims "Applies to all tabs above".

    The lock acts on click, so that claim is false for its tab and the bar is
    excluded from it rather than left to imply the toggle needs saving.
    """
    page = _SETTINGS_PAGE.read_text()
    assert '{tab === "safety" && <BreakGlassControl clusterId={id} />}' in page
    assert '{tab !== "safety" && (' in page


def test_the_console_reaches_every_jobs_endpoint_it_should():
    """List, cancel and manifest-compare all have a caller.

    /jobs/trigger has no caller because it no longer exists. Tracing showed the
    row it wrote - PENDING, investigation, no payload - is exactly what
    `claim_jobs` selects, so the worker claimed it and dead-lettered it. Both
    sides of that absence are pinned: the console below, the router with it.
    """
    page = _JOBS_PAGE.read_text()
    assert "api.get<Job[]>(`/clusters/${id}/jobs`)" in page
    assert "api.post(`/clusters/${id}/jobs/${jobId}/cancel`)" in page
    assert "/manifest/compare/${against}" in page
    assert "/jobs/trigger" not in page
    router = _ROOT / "sre_agent" / "api" / "v1" / "jobs.py"
    assert "/jobs/trigger" not in router.read_text()


def test_the_incident_page_can_read_its_own_flight_recorder():
    """The audit page is cluster-wide, so per-incident tool calls needed a home.

    /transcript is the curated timeline; /logs is the raw `agent_audit_logs`
    trail folded together with the Redis step logs. On demand rather than on
    mount, so the loader is bound to a control and not to an effect: it is the
    one response on the page whose size grows with how long the agent ran.
    """
    page = _INCIDENT_PAGE.read_text()
    assert "api.get<AuditRow[]>(`/incidents/${incidentId}/logs`)" in page
    assert "onClick={loadAudit}" in page


def test_the_jobs_page_is_in_the_rail():
    """A page with no nav entry is only marginally less orphaned than no page."""
    rail = (_DASH / "components" / "console" / "Rail.tsx").read_text()
    assert '{ n: "08", label: "Jobs", seg: "jobs" }' in rail
    # The records section is numbered in sequence, so inserting Jobs has to
    # push everything after it along.
    assert '{ n: "11", label: "Settings", seg: "settings" }' in rail


def test_the_incident_page_reaches_both_ticket_endpoints():
    page = _INCIDENT_PAGE.read_text()
    assert "api.get<Ticket>(`/clusters/${id}/incidents/${incidentId}/ticket`)" in page
    assert "api.post(`/clusters/${id}/incidents/${incidentId}/ticket`" in page


def test_the_ticket_panel_refetches_after_creating():
    """POST /ticket answers with the issue key alone.

    The browse URL is assembled by the GET from the cluster's Jira base, so a
    panel that trusted the POST response would render a bare key with no link.
    """
    page = _INCIDENT_PAGE.read_text()
    create = page.split("const createTicket")[1].split("useEffect")[0]
    assert "await loadTicket()" in create


_STREAM_REFRESH_ONLY = [
    ("slos", _DASH / "app" / "(dashboard)" / "clusters" / "[id]" / "slos" / "page.tsx"),
    (
        "services",
        _DASH / "app" / "(dashboard)" / "clusters" / "[id]" / "services" / "page.tsx",
    ),
    (
        "service detail",
        _DASH
        / "app"
        / "(dashboard)"
        / "clusters"
        / "[id]"
        / "services"
        / "[svc]"
        / "page.tsx",
    ),
    (
        "incidents",
        _DASH / "app" / "(dashboard)" / "clusters" / "[id]" / "incidents" / "page.tsx",
    ),
    ("jobs", _DASH / "app" / "(dashboard)" / "clusters" / "[id]" / "jobs" / "page.tsx"),
    (
        "audit",
        _DASH / "app" / "(dashboard)" / "clusters" / "[id]" / "audit" / "page.tsx",
    ),
    ("cluster layout", _DASH / "app" / "(dashboard)" / "clusters" / "[id]" / "layout.tsx"),
    (
        "analytics",
        _DASH / "app" / "(dashboard)" / "clusters" / "[id]" / "analytics" / "page.tsx",
    ),
    ("overview", _DASH / "app" / "(dashboard)" / "clusters" / "[id]" / "page.tsx"),
]


def test_the_other_stream_consumers_only_count_events_they_never_read_them():
    """Nine cluster pages subscribe to the org-wide incidents feed.

    They are safe for one reason: each uses the stream as a doorbell. They read
    `events.length`, notice it moved, and re-fetch over REST — where the route
    carries a cluster and the server enforces ownership. None of them touches
    `events[i].payload`, so none can render a sibling cluster's data.

    That is a property, not an accident, and it is the property that broke in
    `IncidentToasts` the moment a payload got rendered directly. Asserting it
    here means the next page to read a payload has to justify it.
    """
    offenders = []
    for name, path in _STREAM_REFRESH_ONLY:
        src = path.read_text()
        assert "useLiveStream(" in src, f"{name} no longer subscribes at all"
        if ".payload" in src:
            offenders.append(name)
    assert not offenders, (
        "these pages now read a stream payload directly and must filter it by "
        f"cluster: {offenders}"
    )


def test_the_incident_page_refuses_another_cluster_s_incident():
    """`get_owned_incident` joins on org, not cluster.

    So /clusters/<A>/incidents/<B's incident> loads B's transcript and renders
    it under A's breadcrumb. Only the ticket call is cluster-scoped, so it
    fails on its own while the rest of the page looks correct. The page has the
    cluster on the incident it just fetched; it has to check it.
    """
    src = _INCIDENT_PAGE.read_text()
    assert "if (tx.incident.cluster_id !== id) {" in src
    assert "This incident belongs to a different cluster." in src


def test_a_cluster_page_never_shows_another_cluster_s_health():
    """The insights stream is org-scoped; the page it feeds is not.

    `/ws/insights` is filtered by `event_visible_to_org`, so it carries every
    cluster the organization owns. This page used to take the newest snapshot
    for *this* cluster `?? snapshots[0]` — and with no snapshot of its own it
    rendered a sibling's services, error rates and p95s under this cluster's
    heading. The filter has to be in the memo, so it applies to the sweep
    feed below as well as the table.
    """
    src = _INSIGHTS_PAGE.read_text()
    assert 'p.kind === "cluster_health" && p.cluster_id === id' in src
    assert "?? snapshots[0]" not in src, "the cross-cluster fallback is back"


def test_the_insights_memo_recomputes_when_the_cluster_changes():
    """A cluster-dependent memo with a stale dep list is the same bug again."""
    src = _INSIGHTS_PAGE.read_text()
    assert "}, [events, id])" in src


def test_incident_toasts_are_scoped_to_the_cluster_on_screen():
    """Toasts are mounted in the cluster layout, so they are on every page.

    An unscoped event cannot be placed and is dropped rather than shown. The
    match against the current cluster happens at render, not at ingest,
    because Next reuses this layout across sibling `[id]` routes: `id` changes
    without a remount, so an ingest-time filter would read a stale closure and
    already-queued toasts would strand on the wrong cluster's page.
    """
    src = _TOASTS.read_text()
    assert 'const clusterId = typeof p.cluster_id === "string" ? p.cluster_id : undefined' in src
    assert "if (!clusterId) continue" in src
    assert "const mine = toasts.filter((t) => t.clusterId === id)" in src
    assert "if (mine.length === 0) return null" in src


def test_a_toast_cannot_be_built_without_its_cluster():
    """`clusterId` is required on the type, so the drop above cannot be skipped."""
    src = _TOASTS.read_text()
    assert "\n  clusterId: string\n" in src


def test_an_slo_can_be_removed_from_the_console():
    """Create, edit and delete all reachable from the same table.

    `DELETE /clusters/{cluster_id}/slos/{slo_id}` was mounted with no caller,
    so an objective could be created and edited from the UI but removed only
    by hand-rolling an authenticated request. This is also the dashboard's
    first `api.delete`, so it is the idiom the next one should copy.
    """
    src = _SLOS_PAGE.read_text()
    assert "api.delete(`/clusters/${id}/slos/${slo.id}`)" in src


def test_deleting_an_slo_asks_first():
    """Two-step inline confirm, matching the break glass.

    `window.confirm` is deliberately not used anywhere in the console: it is
    unstyleable, it blocks the event loop while the 20s poll is running, and
    it cannot carry the sentence explaining what is actually destroyed.
    """
    src = _SLOS_PAGE.read_text()
    assert "confirmDelete === r.slo.id" in src
    assert "setConfirmDelete(r.slo.id)" in src
    assert "window.confirm" not in src


def test_a_failed_delete_says_the_objective_is_still_there():
    """A row that does not disappear is not an explanation."""
    src = _SLOS_PAGE.read_text()
    assert "setDeleteError(" in src
    assert "It is unchanged." in src


def test_deleting_the_objective_being_edited_closes_its_form():
    """Otherwise the open form saves a PATCH against a deleted SLO."""
    src = _SLOS_PAGE.read_text()
    assert "if (editingId === slo.id) closeForm()" in src


def test_the_delete_confirmation_does_not_claim_alerting_stops():
    """Nothing in the runtime reads the `slos` table.

    Severity is derived from measured evidence and incidents are opened by the
    tenant's own Alertmanager (`agent_runtime.py:519`), so deleting an
    objective removes a tracker and its recorded budget history and nothing
    else. Copy that implied alerts would stop would be false, and would make
    operators keep dead objectives out of fear.
    """
    src = _SLOS_PAGE.read_text()
    assert "Alerting is unaffected." in src
