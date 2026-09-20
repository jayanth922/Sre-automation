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
