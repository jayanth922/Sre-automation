#!/usr/bin/env python3
"""Unit tests for Langfuse tracing wiring (competitive-audit upgrade)."""

import importlib.util
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "sre_agent" / "tracing.py"
_spec = importlib.util.spec_from_file_location("tracing", _MODULE_PATH)
tr = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = tr
_spec.loader.exec_module(tr)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("LANGFUSE_TRACING", "LANGFUSE_PUBLIC_KEY"):
        monkeypatch.delenv(k, raising=False)


def test_enabled_by_default():
    # Self-hosted Langfuse is wired by default in every deployment; no env
    # var is required to opt in.
    assert tr.langfuse_enabled() is True


def test_disabled_via_explicit_opt_out(monkeypatch):
    monkeypatch.setenv("LANGFUSE_TRACING", "false")
    assert tr.langfuse_enabled() is False
    assert tr.get_langfuse_callback() is None


def test_enabled_by_public_key(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-x")
    assert tr.langfuse_enabled() is True


def test_tracing_callbacks_passthrough_when_disabled(monkeypatch):
    monkeypatch.setenv("LANGFUSE_TRACING", "false")
    base = {"callbacks": ["existing"]}
    assert tr.tracing_callbacks(base) is base
    assert tr.tracing_callbacks(None) is None


def test_tracing_callbacks_appends_handler_when_enabled(monkeypatch):
    monkeypatch.setenv("LANGFUSE_TRACING", "true")
    monkeypatch.setattr(tr, "get_langfuse_callback", lambda org_langfuse=None: "LF_HANDLER")
    cfg = tr.tracing_callbacks({"callbacks": ["existing"], "configurable": {"thread_id": "i1"}})
    assert cfg["callbacks"] == ["existing", "LF_HANDLER"]
    assert cfg["configurable"] == {"thread_id": "i1"}  # base preserved


def test_flush_is_safe_when_disabled():
    tr.flush()  # no exception


def test_get_langfuse_callback_org_without_keys_is_untraced(monkeypatch):
    # An org that exists but hasn't configured Langfuse gets no tracing and no
    # fallback to the operator's env vars (no cross-tenant trace mixing).
    monkeypatch.setenv("LANGFUSE_TRACING", "true")
    assert tr.get_langfuse_callback({"public_key": "", "secret_key": ""}) is None
    assert tr.get_langfuse_callback({"public_key": None, "secret_key": None}) is None


def test_tracing_callbacks_org_without_keys_passes_through(monkeypatch):
    monkeypatch.setenv("LANGFUSE_TRACING", "true")
    base = {"callbacks": ["existing"]}
    result = tr.tracing_callbacks(base, org_langfuse={"public_key": "", "secret_key": ""})
    assert result is base


# ---------------------------------------------------------------------------
# redact() — the secret/PII scrubber behind the export-stage masking hook
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, must_not_contain, marker",
    [
        ("Authorization: Bearer abcd1234efgh5678", "abcd1234efgh5678", "[REDACTED]"),
        ("slack token xoxb-123456789012-abcdefghijkl", "xoxb-123456789012", "[REDACTED:slack-token]"),
        ("export GITHUB=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", "ghp_ABCDEFGH", "[REDACTED:github-token]"),
        ("aws key AKIAIOSFODNN7EXAMPLE failed", "AKIAIOSFODNN7EXAMPLE", "[REDACTED:aws-access-key-id]"),
        ("LANGFUSE_SECRET_KEY=sk-lf-11111111-2222-3333-4444-555555555555", "sk-lf-1111", "[REDACTED:langfuse-key]"),
        ("psql postgres://svc:hunter2@db.internal:5432/app", "hunter2", "[REDACTED]"),
        ("paged oncall@example.com about it", "oncall@example.com", "[REDACTED:email]"),
    ],
)
def test_redact_scrubs_known_secret_shapes(raw, must_not_contain, marker):
    masked = tr.redact(raw)
    assert must_not_contain not in masked
    assert marker in masked


def test_redact_leaves_ordinary_incident_text_alone():
    # False positives are expensive here: a scrubbed log line is a trace nobody
    # can debug from. Ordinary SRE prose must survive untouched.
    text = "checkout-service p99 latency 2400ms, 3 pods CrashLoopBackOff in prod"
    assert tr.redact(text) == text


# ---------------------------------------------------------------------------
# mask_otel_spans() — the hook handed to Langfuse(mask_otel_spans=...)
# ---------------------------------------------------------------------------


def _mask(attributes):
    """Call the hook with one span carrying ``attributes``; return its patch."""
    from types import SimpleNamespace

    identifier = ("trace-1", "span-1")
    params = SimpleNamespace(spans={identifier: SimpleNamespace(attributes=attributes)})
    result = tr.mask_otel_spans(params=params)
    if result is None:
        return None
    return result.span_patches.get(identifier)


def test_mask_otel_spans_patches_only_attributes_that_changed():
    pytest.importorskip("langfuse.types")
    patch = _mask(
        {
            "input.value": "curl -H 'Authorization: Bearer sekret-token-value'",
            "gen_ai.request.model": "claude-opus-5",
        }
    )
    assert patch is not None
    assert "sekret-token-value" not in patch.set_attributes["input.value"]
    # Sparse patch: the clean attribute is absent, so it exports unchanged.
    assert "gen_ai.request.model" not in patch.set_attributes
    assert patch.set_attributes["langfuse.masking.applied"] is True


def test_mask_otel_spans_returns_none_when_nothing_is_sensitive():
    pytest.importorskip("langfuse.types")
    assert _mask({"gen_ai.request.model": "claude-opus-5"}) is None


def test_mask_otel_spans_masks_inside_string_sequences():
    pytest.importorskip("langfuse.types")
    patch = _mask({"tool.args": ["kubectl get pods", "token=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"]})
    assert patch is not None
    assert patch.set_attributes["tool.args"][0] == "kubectl get pods"
    assert "ghp_ABCDEFGH" not in patch.set_attributes["tool.args"][1]


def test_mask_otel_spans_clips_oversized_payloads(monkeypatch):
    # Every LangGraph node run carries the whole AgentState as input *and*
    # output; unclipped, one node exported 600 KB and risked silent rejection
    # at the ingestion limit.
    pytest.importorskip("langfuse.types")
    monkeypatch.setenv("LANGFUSE_MAX_ATTRIBUTE_CHARS", "100")
    patch = _mask({"langfuse.observation.input": "a" * 5000})
    assert patch is not None
    clipped = patch.set_attributes["langfuse.observation.input"]
    assert clipped.startswith("a" * 100)
    assert "truncated 4900 chars" in clipped


def test_mask_otel_spans_clipping_can_be_disabled(monkeypatch):
    pytest.importorskip("langfuse.types")
    monkeypatch.setenv("LANGFUSE_MAX_ATTRIBUTE_CHARS", "0")
    assert _mask({"langfuse.observation.input": "a" * 5000}) is None


def test_mask_otel_spans_never_raises():
    # The SDK drops the entire export batch if this hook raises, so a malformed
    # batch must degrade to "export unmasked", never to "lose every span".
    from types import SimpleNamespace

    assert tr.mask_otel_spans(params=SimpleNamespace()) is None
    assert tr.mask_otel_spans(params=None) is None


# ---------------------------------------------------------------------------
# _should_export_span() — the noise filter handed to Langfuse(...)
# ---------------------------------------------------------------------------


def _span(name):
    from types import SimpleNamespace

    return SimpleNamespace(name=name, attributes={}, instrumentation_scope=None)


@pytest.mark.parametrize(
    "name", ["Prompt", "PydanticToolsParser", "call_model", "should_continue", "_route_supervisor"]
)
def test_should_export_span_drops_always_leaf_framework_plumbing(name):
    assert tr._should_export_span(_span(name)) is False


@pytest.mark.parametrize("name", ["RunnableSequence", "agent", "tools", "LangGraph"])
def test_should_export_span_keeps_spans_that_parent_other_observations(name):
    # The SDK drops spans without re-parenting their children, so anything
    # that owns a generation or a tool call has to survive the filter.
    pytest.importorskip("langfuse.span_filter")
    from types import SimpleNamespace

    span = SimpleNamespace(
        name=name,
        attributes={},
        instrumentation_scope=SimpleNamespace(name="langfuse-sdk", attributes={}),
    )
    assert tr._should_export_span(span) is True


def test_should_export_span_fails_open():
    # A filter bug should cost a noisy trace, never a missing one.
    assert tr._should_export_span(object()) is True


def test_release_prefers_explicit_override(monkeypatch):
    tr._release.cache_clear()
    monkeypatch.setenv("LANGFUSE_RELEASE", "v2.1.0")
    assert tr._release() == "v2.1.0"
    tr._release.cache_clear()


def test_release_falls_back_to_the_run_manifest_code_sha(monkeypatch):
    # One source of truth for "which commit produced this run": a trace's
    # release and a run manifest's code_sha must never disagree.
    tr._release.cache_clear()
    monkeypatch.delenv("LANGFUSE_RELEASE", raising=False)
    monkeypatch.setattr(
        "sre_agent.run_manifest._resolve_code_sha", lambda *a, **k: "deadbeef"
    )
    assert tr._release() == "deadbeef"
    tr._release.cache_clear()


def test_release_is_cached_so_the_git_fallback_runs_once(monkeypatch):
    # _resolve_client() runs on every invocation and the resolver shells out
    # to git when no build-time SHA was baked in.
    tr._release.cache_clear()
    monkeypatch.delenv("LANGFUSE_RELEASE", raising=False)
    calls = []

    def _counting(*a, **k):
        calls.append(1)
        return "abc1234"

    monkeypatch.setattr("sre_agent.run_manifest._resolve_code_sha", _counting)
    assert tr._release() == "abc1234"
    assert tr._release() == "abc1234"
    assert len(calls) == 1
    tr._release.cache_clear()


def test_release_survives_a_broken_resolver(monkeypatch):
    tr._release.cache_clear()
    monkeypatch.delenv("LANGFUSE_RELEASE", raising=False)

    def _boom(*a, **k):
        raise RuntimeError("no git here")

    monkeypatch.setattr("sre_agent.run_manifest._resolve_code_sha", _boom)
    assert tr._release() is None
    tr._release.cache_clear()


# ---------------------------------------------------------------------------
# trace_attributes() — the reserved keys the LangChain handler lifts into
# propagate_attributes() at the root chain run
# ---------------------------------------------------------------------------


def test_trace_attributes_sets_reserved_keys():
    attrs = tr.trace_attributes(
        "investigate-incident",
        session_id="inc-1",
        user_id="u-9",
        trigger="alert",
        tags=["service:checkout"],
        metadata={"alert_name": "CheckoutHighErrorRate"},
    )
    assert attrs["langfuse_trace_name"] == "investigate-incident"
    assert attrs["langfuse_session_id"] == "inc-1"
    assert attrs["langfuse_user_id"] == "u-9"
    assert attrs["langfuse_tags"] == ["service:checkout", "trigger:alert"]
    assert attrs["alert_name"] == "CheckoutHighErrorRate"


def test_trace_attributes_name_is_stable_across_runs():
    # Names drive evaluators, dashboards and saved filters, so they must not
    # carry run-specific ids — two runs of the same operation share a name.
    a = tr.trace_attributes("investigate-incident", session_id="inc-1")
    b = tr.trace_attributes("investigate-incident", session_id="inc-2")
    assert a["langfuse_trace_name"] == b["langfuse_trace_name"]
    assert a["langfuse_session_id"] != b["langfuse_session_id"]


def test_trace_attributes_derives_tags_from_execution_context():
    from types import SimpleNamespace

    attrs = tr.trace_attributes(
        "investigate-incident",
        context=SimpleNamespace(cluster_id="c-1", namespace="prod", llm_model="claude-opus-5"),
    )
    assert "cluster:c-1" in attrs["langfuse_tags"]
    assert attrs["cluster_id"] == "c-1"
    assert attrs["namespace"] == "prod"
    assert attrs["llm_model"] == "claude-opus-5"


def test_trace_attributes_drops_empty_metadata_and_clips_long_values():
    # propagate_attributes coerces values to strings and caps them at 200
    # characters; clipping here keeps the truncation visible instead of silent.
    attrs = tr.trace_attributes(
        "investigate-incident",
        metadata={"job_id": None, "blank": "   ", "long": "x" * 500},
    )
    assert "job_id" not in attrs
    assert "blank" not in attrs
    assert len(attrs["long"]) == 200
    assert attrs["long"].endswith("…")


def test_trace_attributes_omits_reserved_keys_it_has_no_value_for():
    attrs = tr.trace_attributes("answer-sre-query")
    assert "langfuse_session_id" not in attrs
    assert "langfuse_user_id" not in attrs
    assert "langfuse_tags" not in attrs


# ---------------------------------------------------------------------------
# trace_run() — the curated root observation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trace_run_is_a_no_op_when_tracing_is_disabled(monkeypatch):
    monkeypatch.setenv("LANGFUSE_TRACING", "false")
    async with tr.trace_run("investigate-incident", input={"alert": "x"}) as run:
        run.set_output({"summary": "done"})  # must not raise without a client


@pytest.mark.asyncio
async def test_trace_run_still_propagates_the_wrapped_error(monkeypatch):
    # Tracing records the failure, it never swallows it: the caller's error
    # handling (HTTP 500, Redis ERROR state) has to keep working.
    monkeypatch.setenv("LANGFUSE_TRACING", "false")
    with pytest.raises(ValueError):
        async with tr.trace_run("investigate-incident"):
            raise ValueError("graph exploded")


@pytest.mark.asyncio
async def test_trace_run_accepts_trace_attributes_for_graphless_call_sites(monkeypatch):
    # A direct reply answers from context, so no LangChain handler runs and
    # trace_run is the only thing that can name the trace and file it under
    # the incident's session.
    monkeypatch.setenv("LANGFUSE_TRACING", "false")
    async with tr.trace_run(
        "answer-incident-follow-up",
        session_id="inc-1",
        user_id="u-9",
        tags=["trigger:slack"],
    ) as run:
        run.set_output({"answer": "ok"})


# ---------------------------------------------------------------------------
# mark_current_observation() — making failed work findable
# ---------------------------------------------------------------------------


class _RecordingSpan:
    """Minimal OTel span stand-in that records what was set on it."""

    def __init__(self, attributes=None):
        self.attributes = dict(attributes or {})

    def is_recording(self):
        return True

    def set_attribute(self, key, value):
        self.attributes[key] = value


def _install_span(monkeypatch, span):
    from types import SimpleNamespace

    monkeypatch.setitem(
        sys.modules,
        "opentelemetry",
        SimpleNamespace(trace=SimpleNamespace(get_current_span=lambda: span)),
    )
    monkeypatch.setitem(
        sys.modules,
        "opentelemetry.trace",
        SimpleNamespace(get_current_span=lambda: span),
    )


_LEVEL = "langfuse.observation.level"
_STATUS = "langfuse.observation.status_message"


def test_mark_current_observation_flags_a_refused_action(monkeypatch):
    span = _RecordingSpan()
    _install_span(monkeypatch, span)
    tr.mark_current_observation("WARNING", "patch_resource_limits → REFUSED: no memory/cpu")
    assert span.attributes[_LEVEL] == "WARNING"
    assert "REFUSED" in span.attributes[_STATUS]


def test_mark_current_observation_never_downgrades_a_worse_level(monkeypatch):
    # One action erroring and a later one merely being refused must not leave
    # the step looking like the milder failure.
    span = _RecordingSpan({_LEVEL: "ERROR"})
    _install_span(monkeypatch, span)
    tr.mark_current_observation("WARNING", "later, milder failure")
    assert span.attributes[_LEVEL] == "ERROR"


def test_mark_current_observation_keeps_naming_every_failed_action(monkeypatch):
    # One call arrives per failed action, all of them landing on the same
    # (run-level) observation. Overwriting would leave a four-refusal run
    # reporting only the last refusal.
    span = _RecordingSpan()
    _install_span(monkeypatch, span)
    tr.mark_current_observation("WARNING", "patch_resource_limits → REFUSED: no memory")
    tr.mark_current_observation("ERROR", "restart_deployment → ERROR: timeout")
    assert "patch_resource_limits" in span.attributes[_STATUS]
    assert "restart_deployment" in span.attributes[_STATUS]
    assert span.attributes[_LEVEL] == "ERROR"


def test_mark_current_observation_records_a_milder_later_failure_too(monkeypatch):
    # The worse level wins, but the milder failure still has to be named —
    # it is a second broken action, not a duplicate of the first.
    span = _RecordingSpan({_LEVEL: "ERROR", _STATUS: "restart_deployment → ERROR"})
    _install_span(monkeypatch, span)
    tr.mark_current_observation("WARNING", "scale_deployment → REFUSED")
    assert span.attributes[_LEVEL] == "ERROR"
    assert "scale_deployment" in span.attributes[_STATUS]
    assert "restart_deployment" in span.attributes[_STATUS]


def test_mark_current_observation_does_not_repeat_an_identical_failure(monkeypatch):
    span = _RecordingSpan()
    _install_span(monkeypatch, span)
    tr.mark_current_observation("WARNING", "patch_resource_limits → REFUSED")
    tr.mark_current_observation("WARNING", "patch_resource_limits → REFUSED")
    assert span.attributes[_STATUS].count("patch_resource_limits") == 1


def test_mark_current_observation_escalates_to_a_worse_level(monkeypatch):
    span = _RecordingSpan({_LEVEL: "WARNING"})
    _install_span(monkeypatch, span)
    tr.mark_current_observation("ERROR", "now it actually broke")
    assert span.attributes[_LEVEL] == "ERROR"


def test_mark_current_observation_redacts_the_status_message(monkeypatch):
    span = _RecordingSpan()
    _install_span(monkeypatch, span)
    tr.mark_current_observation("ERROR", "auth failed for token ghp_" + "a" * 36)
    assert "ghp_" not in span.attributes[_STATUS]


def test_mark_current_observation_ignores_a_span_that_is_not_recording(monkeypatch):
    span = _RecordingSpan()
    span.is_recording = lambda: False
    _install_span(monkeypatch, span)
    tr.mark_current_observation("ERROR", "dropped")
    assert _LEVEL not in span.attributes


def test_mark_current_observation_never_raises(monkeypatch):
    from types import SimpleNamespace

    def _boom():
        raise RuntimeError("no otel context")

    monkeypatch.setitem(
        sys.modules, "opentelemetry.trace", SimpleNamespace(get_current_span=_boom)
    )
    monkeypatch.setitem(
        sys.modules,
        "opentelemetry",
        SimpleNamespace(trace=SimpleNamespace(get_current_span=_boom)),
    )
    tr.mark_current_observation("ERROR", "tracing must never break execution")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
