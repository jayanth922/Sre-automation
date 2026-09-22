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

    /jobs/trigger deliberately does not: a durable job is created by the alert
    pipeline, and hand-starting one from the console would produce an
    investigation with no incident behind it.
    """
    page = _JOBS_PAGE.read_text()
    assert "api.get<Job[]>(`/clusters/${id}/jobs`)" in page
    assert "api.post(`/clusters/${id}/jobs/${jobId}/cancel`)" in page
    assert "/manifest/compare/${against}" in page
    assert "/jobs/trigger" not in page


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
