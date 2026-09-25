#!/usr/bin/env python3
"""Deterministic corrections stapled underneath the supervisor's own words.

Two failures keep coming back in follow-up answers, and both are about the
same thing: the model narrating a phase the incident is not in.

On `555a3acb` seq 15 — the first real in-thread Slack question this platform
ever answered — the reply was "we're still in the investigation phase right
now — the incident is marked `awaiting_approval`, and the execution graph just
started". Three claims, one sentence, and they contradict each other *and* the
database: the investigation was finished, nothing was executing, and a plan
was sitting in front of a human waiting to be approved. The answer then
offered a walkthrough and **never named `approve fix`**, the one reply that
would have moved anything. Slack is the only channel this product has, so an
answer that does not name the command is an answer that strands the incident.

Asking the model not to do this does not work — the same bar #23 set, and
eight of eight replays failed it for `_echoed_alert_claims`. The incident's
status is a fact in a column, so the correction is computed from it and
appended below the narration, the way
`incident_timeline.build_supervisor_summary_content` appends its echoed-claim
caveat. The model's text is left exactly as written; the reader gets the truth
underneath it either way.

Deliberately narrow, on the same reasoning as `_echoed_alert_claims`:
`_PHASE_CLAIMS` only matches phrasings that assert the *current* phase
outright. "Once the investigation finishes" and "the investigation found X"
are not claims about now, so they do not match, and a near-miss costs a
correction that is merely redundant rather than one that is wrong.
"""

from __future__ import annotations

import re
from typing import Dict, List, NamedTuple, Optional


class StatusFacts(NamedTuple):
    """What a recorded status means, in the words the correction uses."""

    phase: str
    happening: str
    command: Optional[str]
    command_effect: str


# Keyed by `IncidentStatus.value`, which is what `build_incident_chat_context`
# puts in `incident_status`.
_STATUS_FACTS: Dict[str, StatusFacts] = {
    "open": StatusFacts(
        phase="investigating",
        happening="the incident is open and no investigation has reached a conclusion yet",
        command=None,
        command_effect="",
    ),
    "investigating": StatusFacts(
        phase="investigating",
        happening="the investigation is still running",
        command=None,
        command_effect="",
    ),
    "investigated": StatusFacts(
        phase="waiting",
        happening=(
            "the investigation is finished and nothing was changed on the cluster"
        ),
        command="mark resolved",
        command_effect="close this incident",
    ),
    "awaiting_approval": StatusFacts(
        phase="waiting",
        happening=(
            "the investigation is finished and a remediation plan is waiting on "
            "a human to approve it — nothing is executing"
        ),
        command="approve fix",
        command_effect="run the plan",
    ),
    "remediation_in_progress": StatusFacts(
        phase="remediating",
        happening="the approved remediation is executing right now",
        command=None,
        command_effect="",
    ),
    "remediation_failed": StatusFacts(
        phase="stopped",
        happening=(
            "the last remediation attempt failed, so the cluster was not fixed"
        ),
        command="mark resolved",
        command_effect="close this incident",
    ),
    "verification_unknown": StatusFacts(
        phase="stopped",
        happening=(
            "a remediation ran and nobody ever confirmed whether it worked"
        ),
        command="mark resolved",
        command_effect="close this incident",
    ),
    "pending_acknowledgment": StatusFacts(
        # Not `finished`: the fix landed but the incident is still open, so
        # "this incident is resolved" is a claim the status contradicts.
        phase="acknowledging",
        happening=(
            "a fix was applied and verified, and the incident stays open until "
            "someone acknowledges it"
        ),
        command="acknowledge",
        command_effect="close this incident",
    ),
    "resolved": StatusFacts(
        phase="finished",
        happening="the incident is closed",
        command=None,
        command_effect="",
    ),
}

# Phrasings that assert what is happening *now*. Each entry is the phase the
# phrasing claims. Anything conditional, past-tense or hedged is left out on
# purpose — see the module docstring.
_PHASE_CLAIMS: List[tuple] = [
    ("investigating", re.compile(
        r"\b(?:still|currently|right now)\s+(?:in\s+the\s+)?investigat", re.I)),
    ("investigating", re.compile(
        r"\bin\s+the\s+investigation\s+(?:phase|stage)\b", re.I)),
    ("investigating", re.compile(
        r"\binvestigation\s+is\s+(?:still\s+)?(?:ongoing|running|under\s?way|underway|in\s+progress)\b", re.I)),
    ("investigating", re.compile(
        r"\b(?:specialists|agents)\s+are\s+(?:still\s+)?(?:working|running|digging)\b", re.I)),
    ("remediating", re.compile(
        r"\b(?:execution\s+graph|remediation|fix)\s+(?:has\s+)?(?:just\s+)?(?:started|kicked\s+off|is\s+running|is\s+executing)\b", re.I)),
    ("remediating", re.compile(
        r"\b(?:currently|right now)\s+(?:executing|applying\s+the\s+fix|remediating)\b", re.I)),
    ("remediating", re.compile(
        r"\bwe(?:'re| are)\s+(?:now\s+)?(?:executing|applying\s+the\s+fix|remediating)\b", re.I)),
    ("finished", re.compile(
        r"\b(?:this\s+)?incident\s+(?:is|has\s+been)\s+(?:now\s+)?(?:resolved|closed)\b", re.I)),
]

# One per phase that `_PHASE_CLAIMS` can produce; the pairing is asserted in
# `tests/test_narration_grounding.py` so a new claim cannot land without one.
_PHASE_LABEL = {
    "investigating": "an investigation is still running",
    "remediating": "a remediation is executing",
    "finished": "the incident is closed",
}


def claimed_phases(text: str) -> List[str]:
    """Phases the text asserts as current, in the order they first appear."""
    hits: List[str] = []
    for phase, pattern in _PHASE_CLAIMS:
        if phase not in hits and pattern.search(text or ""):
            hits.append(phase)
    return hits


def _names_command(text: str, command: str) -> bool:
    """Is the command actually spelled out, not merely gestured at?

    `approve fix` is an exact-match reply (`war_room.FIX_APPROVAL_COMMAND_RE`),
    so "you can approve it" and "approve the fix" do not count — gesturing at
    the idea is precisely the answer that stranded `555a3acb`. The separator
    allows backticks and a line break because the narration formats the
    command as code about half the time, but it has to be the two words
    adjacent.
    """
    pattern = r"[\s`]+".join(re.escape(word) for word in command.split())
    return re.search(pattern, text or "", re.I) is not None


def grounding_footnote(text: str, incident_status: str) -> Optional[str]:
    """The correction to staple under `text`, or None if it needs none.

    Two separate corrections, combined into one block so the thread never
    carries two consecutive appendices for the same message:

    * the narration claims a phase the recorded status contradicts;
    * the status has a reply that moves it and the narration never names it.
    """
    status = (incident_status or "").strip().lower()
    facts = _STATUS_FACTS.get(status)
    if facts is None:
        # An unrecognised status is not grounds for asserting anything about
        # the incident. Say nothing rather than correct it wrongly.
        return None

    parts: List[str] = []

    contradicted = [p for p in claimed_phases(text) if p != facts.phase]
    if contradicted:
        claims = " and ".join(_PHASE_LABEL[p] for p in contradicted)
        parts.append(
            f"⚠️ **The message above says {claims}; the incident's recorded "
            f"status is `{status}`, which means {facts.happening}.** The "
            "status is what the rest of the system acts on."
        )

    if facts.command and not _names_command(text, facts.command):
        # Every status that has a command is by definition one the platform
        # will not leave on its own, so the lead-in is true for all of them —
        # and it is the half the reader actually needs. #29 was the same
        # shape one status earlier: an incident nothing was working on, in a
        # thread that never said so.
        parts.append(
            f"👉 **Nothing further will happen on this incident on its own — "
            f"reply `{facts.command}` in this thread to "
            f"{facts.command_effect}.** It has to be that exact reply: no "
            "other wording moves the incident, and no other part of the "
            "system is going to move it for you."
        )

    if not parts:
        return None
    return "\n\n".join(parts)


def ground_narration(text: str, incident_status: str) -> str:
    """`text` with its correction appended, or unchanged if it needs none.

    The model's own words are never edited — a rewrite is another chance to
    lose the meaning, and the appendix is auditable in a way an edit is not.
    """
    footnote = grounding_footnote(text, incident_status)
    if not footnote:
        return text
    return "\n".join([text.rstrip(), "", "---", footnote])
