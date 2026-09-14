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

Replaying d3ca5138's real evidence through the deployed prompt showed the
first fix was not enough. The narrator stopped quoting the annotation
directly and then wrote "confirmed ... climbed past the 256 Mi pod limit" and
"likely triggered an OOMKill" anyway — because the Prometheus Specialist's
own finding says "well past the 256Mi pod limit ... which lines up with an
OOMKill". Specialists are handed the same annotations, quote them back, and
the quotation returns to the supervisor wearing a specialist's name. Ground
truth: the limit is 768Mi and the container's last termination reason is
`Error` (exit 255) — there has never been an OOMKill.

Two more leaks closed here:
  * the specialist brief must make a specialist mark its own quotations, and
  * the synthesis prompt called the numbers scraped out of the annotation
    prose "numeric facts", which is how a hand-typed "256Mi" acquired the
    standing of a measurement.
"""

from __future__ import annotations

import inspect
import re

from sre_agent import narrative


def _collapse(source: str) -> str:
    """Adjacent string literals as the model receives them."""
    joined = re.sub(r'"\s*\n\s*"', "", source)
    return re.sub(r"\s+", " ", joined)


def _supervisor_rules() -> str:
    """The prompt as the model receives it, not as it is typed.

    The rules are written as adjacent string literals across many source
    lines, so a sentence that reads as one phrase to the model is split by
    quotes and indentation in the file. Join the literals and collapse
    whitespace before asserting on wording.
    """
    return _collapse(inspect.getsource(narrative.narrate_supervisor_summary))


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


D3CA5138_ANNOTATIONS = {
    "summary": "checkout-service memory approaching pod limit",
    "description": (
        "checkout-service simulated heap is 226.1MiB, above 200MB "
        "(pod limit is 256Mi) — an OOMKill is imminent unless the leak "
        "is reverted."
    ),
}

# Verbatim from d3ca5138's timeline. Both echoes are in the first sentence.
D3CA5138_METRICS_FINDING = (
    "Confirmed the memory leak — process_resident_memory_bytes on "
    "checkout-service climbed from ~101.8 MiB flat to a ~305.2 MiB plateau "
    "in about 3 minutes, well past the 256Mi pod limit, then froze flat, "
    "which lines up with an OOMKill."
)


def _alert(annotations=None, **kw):
    from sre_agent.agent_state import AlertContext

    return AlertContext(
        alert_name=kw.pop("alert_name", "CheckoutMemoryApproachingLimit"),
        severity=kw.pop("severity", "critical"),
        labels=kw.pop("labels", {"service": "checkout-service"}),
        annotations=(
            D3CA5138_ANNOTATIONS if annotations is None else annotations
        ),
        **kw,
    )


def test_the_live_failure_is_detected_without_asking_the_model():
    """The exact pair that four replays of the real evidence got wrong."""
    echoed = narrative._echoed_alert_claims(
        _alert(), {"metrics_agent": D3CA5138_METRICS_FINDING}
    )
    squashed = {narrative._squash(item) for item in echoed}
    assert "256mi" in squashed
    assert "oomkill" in squashed


def test_a_figure_the_alert_states_and_nobody_repeats_is_not_echoed():
    """200MB is in the alert and in no finding — the narrator may still use
    it as the threshold that fired, which is exactly what it is."""
    echoed = narrative._echoed_alert_claims(
        _alert(), {"metrics_agent": D3CA5138_METRICS_FINDING}
    )
    assert not any(narrative._squash(item) == "200mb" for item in echoed)


def test_a_measurement_the_alert_never_mentions_is_not_echoed():
    """305.2 MiB came off a graph. Flagging it would teach the narrator to
    distrust the one number in the message that was actually measured."""
    echoed = narrative._echoed_alert_claims(
        _alert(), {"metrics_agent": D3CA5138_METRICS_FINDING}
    )
    assert not any("305" in item for item in echoed)


def test_spacing_and_punctuation_do_not_hide_an_echo():
    """A specialist writing "256 Mi" or "OOM-kill" is making the same claim."""
    echoed = narrative._echoed_alert_claims(
        _alert(),
        {"metrics_agent": "sat at 256 Mi, consistent with an OOM-kill event"},
    )
    squashed = {narrative._squash(item) for item in echoed}
    assert "256mi" in squashed
    assert "oomkill" in squashed


def test_a_capacity_is_recognised_however_the_author_phrased_it():
    """Narrowing the detector to capacity-words nearly cost it the live case.

    Requiring the figure to sit flush against the capacity word missed "(pod
    limit is 256Mi)" — d3ca5138's own description, the one sentence this
    whole caveat exists for. Alert authors write the figure on either side of
    the word and put filler between, so each of these is the same claim.
    """
    phrasings = {
        "(pod limit is 256Mi) — check it": "256mi",
        "memory limit: 256Mi in the manifest": "256mi",
        "usage 3.9Gi, limit of 4Gi": "4gi",
        "past the 85% limit for the node": "85%",
        "against the 512Mi memory cap": "512mi",
    }
    for description, expected in phrasings.items():
        echoed = narrative._echoed_alert_claims(
            _alert(annotations={"description": description}),
            {"metrics_agent": f"the specialist reported {expected} back"},
        )
        squashed = {narrative._squash(item) for item in echoed}
        assert narrative._squash(expected) in squashed, description


def test_a_measured_value_with_no_capacity_word_is_not_an_echo():
    """The reason the detector is narrow at all.

    A slow-query alert states the live p99 and the specialist measures the
    same p99 — two sources agreeing on a real measurement. Flagging that
    would tell the on-call to distrust the best number in the message.
    """
    echoed = narrative._echoed_alert_claims(
        _alert(
            annotations={
                "description": "inventory-service p99 query latency is 2.5s, "
                "above the 1s objective."
            }
        ),
        {"metrics_agent": "Prometheus shows p99 at 2.5s over the last 10 min."},
    )
    assert echoed == []


# The other half of the same word. `CheckoutMemoryApproachingLimit` guesses
# that an OOMKill is coming; `PodOOMKilled` reads one out of
# `kube_pod_container_status_last_terminated_reason`. Only the guess is a
# claim. Live on de223870 — the first real OOMKill this cluster produced,
# exit 137 — the caveat flagged the observed kill as "not measured", which
# is the one thing in that incident that unambiguously was.
DE223870_ANNOTATIONS = {
    "summary": "receipt-renderer was OOMKilled by the kubelet",
    "description": (
        "The kubelet killed container receipt-renderer in pod "
        "receipt-renderer-74c7dcc57f-grr7c (namespace meridian) for "
        "exceeding its memory limit. This is an observed kill, not a "
        "prediction. The limit it exceeded is whatever the live deployment "
        "spec says — read it from the cluster, this alert does not assert "
        "one."
    ),
}

DE223870_METRICS_FINDING = (
    "Confirmed via kube-state-metrics: receipt-renderer "
    "(receipt-renderer-74c7dcc57f-grr7c, ns meridian) got OOMKilled — "
    "kube_pod_container_status_last_terminated_reason shows OOMKilled "
    "across the whole alert window. Limit is 64Mi, request 32Mi."
)


def test_an_observed_kill_the_alert_reports_is_not_an_echo():
    echoed = narrative._echoed_alert_claims(
        _alert(annotations=DE223870_ANNOTATIONS),
        {"metrics_agent": DE223870_METRICS_FINDING},
    )
    assert echoed == []


def test_a_predicted_kill_is_still_an_echo():
    """The distinction must cut one way only — d3ca5138 stays flagged."""
    echoed = narrative._echoed_alert_claims(
        _alert(), {"metrics_agent": D3CA5138_METRICS_FINDING}
    )
    assert any(narrative._squash(item) == "oomkill" for item in echoed)


def test_prediction_is_judged_in_the_sentence_the_event_appears_in():
    """An alert can predict one thing and report another. A hedge about the
    node says nothing about whether the kill happened."""
    echoed = narrative._echoed_alert_claims(
        _alert(
            annotations={
                "description": (
                    "The container was OOMKilled at 12:06. The node may "
                    "come under memory pressure if this continues."
                )
            }
        ),
        {"metrics_agent": "lastState.terminated.reason is OOMKilled, exit 137"},
    )
    assert echoed == []


def test_a_capacity_figure_is_flagged_whether_or_not_it_is_predicted():
    """Framing changes nothing for a hand-typed limit. "the pod limit is
    256Mi" is stated flatly and is still wrong — that is the whole point."""
    echoed = narrative._echoed_alert_claims(
        _alert(annotations={"description": "The pod limit is 256Mi."}),
        {"metrics_agent": "sat well past the 256Mi pod limit"},
    )
    assert any(narrative._squash(item) == "256mi" for item in echoed)


def test_restart_counts_are_never_treated_as_echoes():
    """A restart count is measurable, so "restart" is not in the vocabulary
    even when the alert's prose predicts one."""
    echoed = narrative._echoed_alert_claims(
        _alert(
            annotations={"description": "the pod will restart under memory pressure"}
        ),
        {"metrics_agent": "kube_pod_container_status_restarts_total shows 5 restarts"},
    )
    assert echoed == []


def test_no_findings_and_no_annotations_are_both_empty():
    assert narrative._echoed_alert_claims(_alert(), {}) == []
    assert narrative._echoed_alert_claims(_alert(annotations={}), {"a": "256Mi"}) == []
    assert narrative._echoed_alert_claims(None, {"a": "256Mi"}) == []


def test_the_echoed_list_is_handed_to_the_model_as_a_forbidden_list():
    """Computing it and not saying what it means would change nothing."""
    rules = _supervisor_rules()
    assert "_echoed_alert_claims" in rules
    assert "ECHOED CLAIMS" in rules
    assert "Not corroboration" in rules


def test_hedged_restatements_are_forbidden_too():
    """Every failing replay hedged — "appears to have hit an OOMKill" — and
    a hedge in a TL;DR is still read as a finding."""
    rules = _supervisor_rules()
    assert "appears to have hit an OOMKill" in rules
    assert "FORBIDDEN" in rules


def test_the_rule_names_the_check_that_would_settle_it():
    """"Unconfirmed" without a next step is not actionable for an on-call."""
    rules = _supervisor_rules()
    assert "kubectl describe pod" in rules
    assert "resources.limits" in rules


def test_a_specialist_echoing_the_annotation_is_not_a_measurement():
    """The leak that survived the first fix.

    The rule has to name the laundering explicitly: the model reads a
    specialist's finding as gathered evidence, and nothing about "well past
    the 256Mi pod limit" announces that the specialist read it off the alert
    rather than off a graph.
    """
    rules = _supervisor_rules()
    assert "repeating the alert's own wording is STILL the alert's wording" in rules
    assert "You cannot tell that apart by reading" in rules
    assert "never corroboration" in rules


def test_the_scraped_numbers_are_not_presented_to_the_model_as_facts():
    """`_extract_alert_evidence` scrapes the annotation prose, so its output
    mixes the live value Prometheus templated in with thresholds and pod
    limits typed by hand. The label on that block is what tells the model how
    far to trust each one."""
    rules = _supervisor_rules()
    assert "Numeric facts already in the alert" not in rules
    assert "MIX of live values and hand-typed thresholds" in rules
    assert "no single one of them is a verified property" in rules


def _specialist_brief_rules() -> str:
    return _collapse(inspect.getsource(narrative.build_specialist_task_brief))


def test_specialists_must_mark_what_they_quote_from_the_alert():
    """Fixing only the supervisor leaves the claim arriving pre-laundered.

    A specialist that writes "well past the 256Mi pod limit" has handed the
    supervisor something indistinguishable from a measurement, and the
    supervisor's rule can no longer help.
    """
    brief = _specialist_brief_rules()
    assert "typed into a rule file, not measurements" in brief
    assert "report only what your tools returned" in brief
    assert "did not verify it" in brief


def test_the_specialist_brief_still_tells_them_to_use_the_alert_to_aim():
    """The annotations remain the best starting point for a query — the rule
    is about what gets reported, not about ignoring the alert."""
    brief = _specialist_brief_rules()
    assert "Use them to aim your queries" in brief


def test_the_specialist_brief_explains_why_an_unmarked_quotation_matters():
    """A rule the model is given a reason for survives paraphrase pressure
    better than a bare prohibition — and the reason here is true: the
    supervisor genuinely cannot tell the two apart."""
    brief = _specialist_brief_rules()
    assert "cannot tell your measurements from your quotations" in brief
    assert "reaches the on-call engineer as fact" in brief
