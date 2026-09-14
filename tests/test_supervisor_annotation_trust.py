#!/usr/bin/env python3
"""An alert's prose is a claim to check, not a fact to repeat.

`_format_alert_block` feeds the `summary` and `description` annotations to the
supervisor's wrap-up in the same block as the alert's measured numbers, and
the prompt's existing HARD RULES tell the model that those numbers "PROVE the
monitoring pipeline worked" and that the alert's hints are "the strongest
root-cause signal". Both rules are right about the *numbers*. Nothing told the
model that the *prose* is a sentence a human typed into a rule file and may be
years stale, so the trust bled across.

Live on 2026-09-14, incident d3ca5138. The rule
`CheckoutMemoryApproachingLimit` fires above 200000000 bytes and its
description reads "... above 200MB (pod limit is 256Mi) — an OOMKill is
imminent unless the leak is reverted." The live limit on
deployment/checkout-service is **768Mi**, confirmed with kubectl. The
Supervisor's TL;DR told the on-call "we're 49 MiB away from the 256 Mi pod
limit and OOMKill" — while its own metrics section in the same message
reported 305.2 MiB, already past the limit it claimed, and the Reflector had
separately refused the claim outright: "the memory alert itself is reported
against a 256Mi threshold that does not match the live 768Mi limit, so
imminent OOMKill is not confirmed."

So the evidence to contradict the annotation was already in hand, produced by
the system itself, and the human-facing narrative asserted the annotation
anyway.
"""

from __future__ import annotations

import inspect
import re

from sre_agent import narrative


def _supervisor_rules() -> str:
    """The prompt as the model receives it, not as it is typed.

    The rules are written as adjacent string literals across many source
    lines, so a sentence that reads as one phrase to the model is split by
    quotes and indentation in the file. Join the literals and collapse
    whitespace before asserting on wording.
    """
    source = inspect.getsource(narrative.narrate_supervisor_summary)
    joined = re.sub(r'"\s*\n\s*"', "", source)
    return re.sub(r"\s+", " ", joined)


def test_annotations_are_marked_as_claims_rather_than_evidence():
    rules = _supervisor_rules()
    assert "CLAIM TO CHECK" in rules
    assert "description" in rules


def test_the_live_measurement_is_given_priority_over_the_annotation():
    """The d3ca5138 failure was a tie the model broke the wrong way."""
    rules = _supervisor_rules()
    assert "live measurement wins" in rules


def test_predictions_in_annotations_may_not_be_restated_as_fact():
    """'OOMKill is imminent' is the rule author's guess, not an observation."""
    rules = _supervisor_rules()
    assert "OOMKill is imminent" in rules
    assert "overstates" in rules


def test_the_numeric_facts_are_still_trusted():
    """The separation must not undo the earlier fix that stopped the model
    blaming the monitoring pipeline whenever a follow-up query came back
    empty."""
    rules = _supervisor_rules()
    assert "PROVES the monitoring pipeline worked" in rules
    assert "FORBIDDEN" in rules


def test_both_numbers_must_be_named_when_they_disagree():
    """Silently preferring the live value would leave the on-call unable to
    see that an alert rule needs fixing — the actual follow-up action."""
    rules = _supervisor_rules()
    assert "name both numbers" in rules
