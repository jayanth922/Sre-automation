#!/usr/bin/env python3
"""A follow-up answer must not contradict the incident's own status.

The live failure this exists to prevent, from `555a3acb` seq 15 — the first
in-thread question this platform ever answered:

    we're still in the investigation phase right now — the incident is marked
    `awaiting_approval`, and the execution graph just started

Every clause of that is wrong, and it is wrong in the expensive direction: an
on-call reading it believes something is running, so they wait. Nothing was
running. A remediation plan was sitting in front of them and the only thing
that would move it was the reply `approve fix`, which the answer never
mentioned. Slack is this platform's only channel, so a follow-up answer that
does not name the command is an incident that stops there.

Telling the narrator the status does not fix this — it *was* told the status
and quoted it inside a sentence that contradicted it. So the correction is
computed from the status column and stapled underneath, and these tests are
written against that computation, not against the model.
"""

from __future__ import annotations

import pytest

from backend import models
from sre_agent import narration_grounding, war_room
from sre_agent.incident_timeline import build_supervisor_direct_answer_content
from sre_agent.narration_grounding import ground_narration, grounding_footnote

# Verbatim, from the incident thread.
LIVE_555A3ACB_SEQ_15 = (
    "Good question — we're still in the investigation phase right now: the "
    "incident is marked `awaiting_approval`, and the execution graph just "
    "started, so I'll have more detail for you shortly. Want me to walk "
    "through what the specialists found so far?"
)


def test_the_live_answer_that_stranded_an_incident_is_corrected():
    grounded = ground_narration(LIVE_555A3ACB_SEQ_15, "awaiting_approval")

    # Both lies are named: nothing is investigating and nothing is executing.
    assert "an investigation is still running" in grounded
    assert "a remediation is executing" in grounded
    assert "`awaiting_approval`" in grounded
    assert "waiting on a human to approve it" in grounded

    # And the one thing the reader has to do.
    assert "reply `approve fix`" in grounded

    # The model's own words survive intact above the correction; a rewrite is
    # another chance to lose the meaning.
    assert grounded.startswith(LIVE_555A3ACB_SEQ_15)
    assert "\n\n---\n" in grounded


def test_an_answer_that_already_names_the_command_is_not_nagged():
    text = (
        "The plan is ready and nothing has been changed on the cluster yet. "
        "Reply `approve fix` in this thread and I'll run it."
    )

    assert ground_narration(text, "awaiting_approval") == text


def test_gesturing_at_approval_is_not_naming_the_command():
    """"You can approve the fix" is the failure, not the fix.

    `war_room.FIX_APPROVAL_COMMAND_RE` is an exact match on `approve fix`.
    An answer that describes approving without spelling out the reply leaves
    the reader guessing at a string the parser will reject.
    """
    text = "Nothing will run until you approve the fix — let me know."

    grounded = ground_narration(text, "awaiting_approval")

    assert "reply `approve fix`" in grounded


def test_a_truthful_answer_still_gets_the_command_it_omitted():
    text = "The investigation is done. Root cause is the 256Mi memory limit."

    grounded = ground_narration(text, "awaiting_approval")

    # No contradiction — it claimed no phase at all — but the reply is still
    # the only thing standing between this incident and a fix.
    assert "The message above says" not in grounded
    assert "reply `approve fix`" in grounded


def test_an_accurate_in_flight_answer_is_left_completely_alone():
    text = "Still digging — the metrics specialist is running a range query now."

    assert ground_narration(text, "investigating") == text


def test_claiming_to_be_investigating_while_remediating_is_corrected():
    text = "We're still investigating; I'll report back."

    grounded = ground_narration(text, "remediation_in_progress")

    assert "an investigation is still running" in grounded
    assert "`remediation_in_progress`" in grounded
    assert "executing right now" in grounded
    # Nothing for the human to do while a remediation runs.
    assert "reply `" not in grounded


def test_calling_an_unacknowledged_incident_closed_is_corrected():
    text = "All good — this incident is resolved."

    grounded = ground_narration(text, "pending_acknowledgment")

    assert "the incident is closed" in grounded
    assert "stays open until someone acknowledges it" in grounded
    assert "reply `acknowledge`" in grounded


def test_a_genuinely_closed_incident_is_left_alone():
    assert (
        ground_narration("This incident is resolved.", "resolved")
        == "This incident is resolved."
    )


@pytest.mark.parametrize(
    "status", ["investigated", "remediation_failed", "verification_unknown"]
)
def test_a_dead_end_status_tells_the_reader_how_to_close_it(status):
    """These three are terminal-but-open: the platform will do nothing more on
    its own, and the thread has to say so or the incident is abandoned in
    place — the same black hole #29 was about, one status later."""
    grounded = ground_narration("Here's what we found.", status)

    assert "reply `mark resolved`" in grounded
    assert "Nothing further will happen on this incident on its own" in grounded


def test_a_forward_looking_sentence_is_not_a_claim_about_now():
    """The matcher is deliberately narrow. A redundant correction is cheap; a
    correction that contradicts a sentence the model got right is not."""
    text = (
        "Once the investigation finishes I'll summarise. The investigation "
        "found a memory limit set well below the working set."
    )

    grounded = ground_narration(text, "awaiting_approval")

    assert "The message above says" not in grounded


def test_an_unknown_status_produces_no_claim_at_all():
    """A status this module does not recognise is not licence to assert
    anything about the incident."""
    for status in ["", "   ", "banana", None]:
        assert ground_narration(LIVE_555A3ACB_SEQ_15, status) == LIVE_555A3ACB_SEQ_15


def test_the_status_is_matched_regardless_of_case():
    """One call site passes a hard-coded label rather than a column value."""
    assert grounding_footnote("We're still investigating.", "INVESTIGATING") is None
    assert grounding_footnote("Here's the plan.", "AWAITING_APPROVAL") is not None


# ---------------------------------------------------------------------------
# The mapping itself
# ---------------------------------------------------------------------------

def test_every_incident_status_has_grounding_facts():
    """A new status that nobody adds here would silently go un-grounded, which
    is exactly how this defect shipped the first time."""
    assert {status.value for status in models.IncidentStatus} == set(
        narration_grounding._STATUS_FACTS
    )


def test_every_claimable_phase_has_a_label():
    claimed = {phase for phase, _pattern in narration_grounding._PHASE_CLAIMS}
    assert claimed == set(narration_grounding._PHASE_LABEL)


def test_every_phase_named_by_a_status_is_a_real_phase():
    phases = {facts.phase for facts in narration_grounding._STATUS_FACTS.values()}
    # Claimable phases are a subset of status phases; a status phase that no
    # phrasing can claim is fine (nothing to contradict), the reverse is not.
    assert set(narration_grounding._PHASE_LABEL) <= phases


def test_every_command_we_tell_people_to_type_is_one_slack_accepts():
    """The whole point is a reply that works. A command this module invents
    would be worse than saying nothing: the reader types it, the parser
    ignores it, and the thread looks broken."""
    accepted = [
        war_room.FIX_APPROVAL_COMMAND_RE,
        war_room.ACK_COMMAND_RE,
        war_room.RESOLVE_COMMAND_RE,
    ]
    commands = {
        facts.command
        for facts in narration_grounding._STATUS_FACTS.values()
        if facts.command
    }
    assert commands, "the mapping lost its commands"
    for command in commands:
        assert any(
            pattern.match(command) for pattern in accepted
        ), f"{command!r} is not a command the war room parses"


def test_a_status_with_a_command_says_what_the_command_does():
    for status, facts in narration_grounding._STATUS_FACTS.items():
        if facts.command:
            assert facts.command_effect, f"{status} names a command with no effect"


# ---------------------------------------------------------------------------
# The wiring: the supervisor's follow-up answers actually go through it
# ---------------------------------------------------------------------------

def test_the_supervisor_direct_answer_is_grounded():
    content, payload = build_supervisor_direct_answer_content(
        "what's happening?",
        LIVE_555A3ACB_SEQ_15,
        "Answered from the existing incident context.",
        narrative=LIVE_555A3ACB_SEQ_15,
        incident_status="awaiting_approval",
    )

    assert "`approve fix`" in content
    assert content == payload["answer"], "the thread and the record must agree"
    assert payload["grounding_footnote"]
    assert payload["incident_status"] == "awaiting_approval"


def test_a_direct_answer_with_no_status_is_unchanged():
    """Back-compat: callers that cannot supply a status still work, and they
    get the old behaviour rather than a guess."""
    content, payload = build_supervisor_direct_answer_content(
        "hi", "Hello.", "basis", narrative="Hello there."
    )

    assert content == "Hello there."
    assert payload["grounding_footnote"] is None
