#!/usr/bin/env python3
"""Langfuse tracing (competitive-audit upgrade #2: real LLM observability).

Swaps the in-process observability recorder for the industry-standard OSS tool.
Langfuse's LangChain integration is a callback handler: attach it to the graph's
invoke config and every LLM/chain/tool span is traced (latency, tokens, cost,
the reasoning trajectory). We keep the lightweight recorder for tests/offline;
this adds real tracing when Langfuse is configured.

Verified against the installed Langfuse Python SDK (v4.15.1) and the current
docs (fetched, not remembered — https://langfuse.com/docs/observability/
best-practices, .../integrations/frameworks/langchain, .../features/masking):

    from langfuse.langchain import CallbackHandler
    handler = CallbackHandler(public_key=...)
    graph.astream(state, config={"callbacks": [handler], "metadata": {...}})

Four things this module is responsible for, beyond "attach a handler":

1. **Trace identity** (``trace_attributes``). The LangChain handler turns the
   ``langfuse_trace_name`` / ``langfuse_session_id`` / ``langfuse_user_id`` /
   ``langfuse_tags`` keys of the invoke config's ``metadata`` into a
   ``propagate_attributes()`` scope covering the whole run (verified in
   ``langfuse/langchain/CallbackHandler.py::_parse_langfuse_trace_attributes``).
   That is the documented way to set trace attributes from LangChain/LangGraph
   without restructuring every call site around a context manager.

   - **name**: stable and verb-first (``investigate-incident``), never
     interpolated with an id — evaluators, dashboards and saved filters target
     names, so a per-run name would make them un-groupable.
   - **session_id**: the incident id. One incident is *several* traces —
     investigate, then (after a human approves in Slack) resume-remediation,
     then any follow-up question — and a session is exactly Langfuse's grouping
     for "a workflow that spans multiple requests with human-in-the-loop steps
     in between".
   - **tags**: cluster and trigger, the dimensions an operator actually slices
     by. The org is *not* a tag: each org traces into its own Langfuse project.
   - **metadata**: the correlating ids (incident, job, run manifest, namespace,
     alert) needed to pivot from a trace back into the platform's own tables.

2. **A meaningful root observation** (``trace_run``). Left alone, the trace root
   is LangGraph's own chain run, whose input/output is the entire graph state —
   a JSON blob no reviewer can read at a glance, and the one thing the
   best-practices page says deserves the most care. ``trace_run`` opens an
   ``agent`` observation around the run with a curated input (the alert, or the
   question asked) and output (the summary//outcome), and LangGraph nests under
   it. It degrades to a no-op whenever tracing is off or unconfigured.

3. **Masking** (``mask_otel_spans``). An SRE agent's spans carry kubectl output,
   pod logs, kubeconfigs and git metadata, so they carry credentials and PII by
   construction. The export-stage hook redacts secret-shaped substrings from
   every exported span attribute before it leaves the process. This is the
   SDK's recommended hook (``mask`` is legacy and only covers data set through
   Langfuse SDK APIs, which would miss everything the LangChain integration
   emits).

4. **Export filtering** (``_should_export_span``). LangGraph emits a span for
   every prompt template, output parser and conditional edge it evaluates.
   A third of a real investigation's observations were that plumbing, so the
   always-leaf ones are filtered at export, composed with the SDK's default
   filter so other OTel instrumentation in the process stays excluded.

5. **Environment, release + flush**. Traces are stamped with the deployment
   environment so staging runs don't pollute production dashboards, with the
   app version so a trace can be pinned to the code that produced it, and
   ``trace_run`` flushes on exit so a run that ends with the process idle
   still ships.

Two credential modes:

- No cluster/org bound (local dev, self-hosted CLI runs —
  ``ExecutionContext.from_environment``): ``org_langfuse=None``, keys read from
  LANGFUSE_HOST/LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY (see .env.example).
- A bound cluster/org (dashboard-managed multi-tenant runs): each org sets its
  own Langfuse project keys via Settings → Team (``Organization.
  langfuse_public_key``/``langfuse_secret_key``/``langfuse_host``, see
  ``sre_agent/api/v1/members.py::set_langfuse_config``). Passed in as
  ``org_langfuse={"public_key": ..., "secret_key": ..., "host": ...}``. Uses
  the SDK's (experimental) multi-project client registry — one ``Langfuse(...)``
  client per public_key, looked up by ``public_key`` — so two orgs' traces never
  land in the same project. An org that hasn't configured Langfuse gets no
  fallback to any operator-wide default project: its runs simply go untraced, so
  one tenant's trace data is never silently routed into another's (or the
  operator's own) project.

Set LANGFUSE_TRACING=false to opt out. Guarded imports throughout so the module
loads without langfuse installed, and so a deployment with tracing nominally on
but no reachable Langfuse instance (e.g. bare local `uv run` with nothing
configured) degrades to a silent no-op rather than failing.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
from functools import lru_cache
from typing import Any, AsyncIterator, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

_DEFAULT_LANGFUSE_HOST = "https://cloud.langfuse.com"

# Langfuse coerces propagated metadata values to strings and caps them at 200
# characters; truncate here so the cap never silently drops a correlating id.
_MAX_METADATA_VALUE = 200


def langfuse_enabled() -> bool:
    return os.getenv("LANGFUSE_TRACING", "true").lower() not in ("false", "0", "no")


# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------

# Ordered: broader/structural rules first, so a PEM block or a connection string
# is collapsed before the generic "key = value" rule chews on its insides.
_SECRET_PATTERNS: Tuple[Tuple["re.Pattern[str]", str], ...] = (
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        "[REDACTED:private-key]",
    ),
    # user:password@host in any connection string (postgres://, redis://, amqp://…).
    (
        re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^\s:/@]+):[^\s/@]+@"),
        r"\1:[REDACTED]@",
    ),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{8,}"), "Bearer [REDACTED]"),
    (
        re.compile(r"(?i)\b((?:proxy-)?authorization)\s*[:=]\s*\S+"),
        r"\1: [REDACTED]",
    ),
    # JWTs — kubeconfig/service-account tokens and most OIDC bearer payloads.
    (
        re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}"),
        "[REDACTED:jwt]",
    ),
    (re.compile(r"\b(?:pk|sk)-lf-[A-Za-z0-9\-]{8,}"), "[REDACTED:langfuse-key]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), "[REDACTED:api-key]"),
    (re.compile(r"\bxox[abeprs]-[A-Za-z0-9\-]{8,}"), "[REDACTED:slack-token]"),
    (re.compile(r"\bxapp-[A-Za-z0-9\-]{8,}"), "[REDACTED:slack-token]"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"), "[REDACTED:github-token]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "[REDACTED:github-token]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED:aws-access-key-id]"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"), "[REDACTED:google-api-key]"),
    # Generic `password|secret|token|api_key = value`, in prose, YAML, JSON or env dumps.
    (
        re.compile(
            r"(?i)\b([a-z0-9_.\-]*(?:password|passwd|secret|api[_\-]?key|token|credential)s?)"
            r"(\"?\s*[:=]\s*\"?)([^\s\"',}]{4,})"
        ),
        r"\1\2[REDACTED]",
    ),
    # Email addresses: PII, and they show up in git blame/commit metadata.
    (
        re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
        "[REDACTED:email]",
    ),
)


def redact(text: str) -> str:
    """Redact secret- and PII-shaped substrings from one string.

    Deliberately shape-based rather than key-name-based: the agent's spans carry
    free-form kubectl/log output, where a leaked token is a bare substring with
    no field name attached to it.
    """
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _max_attribute_chars() -> int:
    """Per-attribute character budget at export time (0 disables clipping).

    Every LangGraph node run carries the *entire* AgentState as both its input
    and its output, so a single investigation exported ~4 MB of near-duplicate
    state — 600 KB on one node. That is past the point of being readable, and
    close enough to Langfuse's per-event ingestion ceiling that the biggest
    observations are the ones at risk of being silently rejected. The content
    itself is not lost: the messages and tool results those blobs duplicate are
    already on the generation and tool observations underneath them.
    """
    try:
        return max(0, int(os.getenv("LANGFUSE_MAX_ATTRIBUTE_CHARS", "24000")))
    except ValueError:
        return 24000


def _clip_attribute(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return f"{text[:limit]}… [truncated {len(text) - limit} chars by sre_agent.tracing]"


def _redact_attribute(value: Any, limit: int = 0) -> Tuple[Any, bool]:
    """Redact (and optionally clip) one OTel attribute; report whether it changed."""
    if isinstance(value, str):
        masked = _clip_attribute(redact(value), limit)
        return masked, masked != value
    if isinstance(value, (list, tuple)) and any(isinstance(v, str) for v in value):
        masked_items = [
            _clip_attribute(redact(v), limit) if isinstance(v, str) else v for v in value
        ]
        return masked_items, masked_items != list(value)
    return value, False


def mask_otel_spans(*, params: Any) -> Optional[Any]:
    """Export-stage masking hook passed to ``Langfuse(mask_otel_spans=...)``.

    Runs on the OpenTelemetry batch-export worker for one batch at a time and
    returns sparse patches — spans whose attributes are already clean are left
    untouched. Must never raise: the SDK drops the entire export batch if it
    does, which would lose traces rather than merely un-mask them.
    """
    try:
        from langfuse.types import MaskOtelSpansResult, OtelSpanPatch

        limit = _max_attribute_chars()
        patches: Dict[Any, Any] = {}
        for identifier, span in params.spans.items():
            changed: Dict[str, Any] = {}
            for key, value in span.attributes.items():
                masked, did_change = _redact_attribute(value, limit)
                if did_change:
                    changed[key] = masked
            if changed:
                changed["langfuse.masking.applied"] = True
                patches[identifier] = OtelSpanPatch(set_attributes=changed)

        return MaskOtelSpansResult(span_patches=patches) if patches else None
    except Exception as e:  # pragma: no cover - defensive; see docstring
        logger.warning(f"Langfuse masking hook failed ({e}); exporting batch unmasked.")
        return None


# ---------------------------------------------------------------------------
# Export filtering
# ---------------------------------------------------------------------------

# LangChain/LangGraph plumbing that is always a *leaf* in this graph: it has no
# input/output a reviewer would read, no cost, and no children that would be
# orphaned by dropping it. Filtering these removed 59 of 181 observations (33%)
# from a real investigation without changing the tree's shape.
#
#   Prompt, PydanticToolsParser  — ChatPromptTemplate / output-parser steps
#   call_model, should_continue  — langgraph.prebuilt ReAct internals; the
#                                  model call itself is the sibling generation
#   _route_supervisor            — this graph's conditional-edge function
#
# Structural spans are deliberately NOT here: ``RunnableSequence``, ``agent``
# and ``tools`` each parent a generation or a tool call, and the SDK's filter
# drops spans without re-parenting their children.
_FRAMEWORK_INTERNAL_SPANS = frozenset(
    {"Prompt", "PydanticToolsParser", "call_model", "should_continue", "_route_supervisor"}
)


def _should_export_span(span: Any) -> bool:
    """``Langfuse(should_export_span=...)``: drop framework noise, keep the rest.

    Composes with the SDK's default filter rather than replacing it, so spans
    from any other OTel instrumentation in the process (FastAPI, SQLAlchemy,
    httpx) stay excluded. Fails open: a bug here should cost us a noisy trace,
    never a missing one.
    """
    try:
        if getattr(span, "name", None) in _FRAMEWORK_INTERNAL_SPANS:
            return False
        from langfuse.span_filter import is_default_export_span

        return bool(is_default_export_span(span))
    except Exception as e:  # pragma: no cover - defensive; see docstring
        logger.debug(f"Langfuse span filter failed ({e}); exporting span.")
        return True


@lru_cache(maxsize=1)
def _release() -> Optional[str]:
    """Deployed code version, so a trace can be pinned to the code that made it.

    Defers to the run manifest's resolver rather than re-deriving it, so a
    trace's ``release`` and a run manifest's ``code_sha`` can never disagree
    about which commit produced a run. ``LANGFUSE_RELEASE`` stays an escape
    hatch for deployments that version by something other than a commit.
    Cached because the resolver shells out to git when no build-time SHA was
    baked in, and ``_resolve_client`` runs on every invocation.
    """
    explicit = (os.getenv("LANGFUSE_RELEASE") or "").strip()
    if explicit:
        return explicit
    try:
        try:
            from .run_manifest import _resolve_code_sha
        except ImportError:  # direct-file unit-test loading has no package context
            from sre_agent.run_manifest import _resolve_code_sha

        return _resolve_code_sha()
    except Exception as e:
        logger.debug(f"Langfuse release resolution failed ({e}); omitting release.")
        return None


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------


def _environment() -> str:
    """Langfuse ``environment`` for this deployment (production/staging/...).

    Reuses the platform's own trusted environment resolution so test traces
    don't pollute production dashboards and evaluations.
    """
    explicit = os.getenv("LANGFUSE_TRACING_ENVIRONMENT", "").strip()
    if explicit:
        return explicit
    try:
        try:
            from .execution_context import operator_cluster_environment
        except ImportError:  # direct-file unit-test loading has no package context
            from sre_agent.execution_context import operator_cluster_environment

        return operator_cluster_environment()
    except Exception:
        return "production"


def _resolve_client(
    org_langfuse: Optional[Mapping[str, Optional[str]]] = None,
) -> Tuple[Optional[Any], Optional[str]]:
    """Return ``(Langfuse client, public_key)`` for this run, or ``(None, None)``.

    Single place that knows the two credential modes *and* the cross-cutting
    client options (masking, environment), so the callback handler and the root
    observation in ``trace_run`` always share one correctly-configured client
    per org. ``Langfuse(...)`` is a per-public-key singleton in v4, so calling
    this on every invocation is a registry lookup, not a new exporter.
    """
    if not langfuse_enabled():
        return None, None
    try:
        from langfuse import Langfuse

        common: Dict[str, Any] = {
            "environment": _environment(),
            "mask_otel_spans": mask_otel_spans,
            "should_export_span": _should_export_span,
        }
        release = _release()
        if release:
            common["release"] = release

        if org_langfuse is None:
            public_key = (os.getenv("LANGFUSE_PUBLIC_KEY") or "").strip() or None
            return Langfuse(**common), public_key

        public_key = (org_langfuse.get("public_key") or "").strip()
        secret_key = (org_langfuse.get("secret_key") or "").strip()
        if not public_key or not secret_key:
            return None, None

        client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=(org_langfuse.get("host") or "").strip() or _DEFAULT_LANGFUSE_HOST,
            **common,
        )
        return client, public_key
    except Exception as e:  # pragma: no cover - only without langfuse installed
        logger.warning(f"Langfuse tracing requested but unavailable ({e}); skipping.")
        return None, None


def get_langfuse_callback(org_langfuse: Optional[Dict[str, Optional[str]]] = None) -> Optional[Any]:
    """Return a Langfuse LangChain CallbackHandler, or None if unavailable/off.

    ``org_langfuse`` distinguishes "no cluster/org at all" (``None`` — legacy
    env-var behavior) from "org exists but hasn't configured Langfuse" (a
    dict with missing/blank keys — no tracing, no env fallback).
    """
    client, public_key = _resolve_client(org_langfuse)
    if client is None:
        return None
    try:
        from langfuse.langchain import CallbackHandler

        # Always name the key when we have one. With more than one project
        # registered, the SDK's keyless lookup returns a *disabled* client
        # rather than guessing — which would silently drop every span.
        return CallbackHandler(public_key=public_key) if public_key else CallbackHandler()
    except Exception as e:  # pragma: no cover - only without langfuse installed
        logger.warning(f"Langfuse tracing requested but unavailable ({e}); skipping.")
        return None


# ---------------------------------------------------------------------------
# Trace identity
# ---------------------------------------------------------------------------


def _clip(value: Any) -> str:
    text = str(value)
    return text if len(text) <= _MAX_METADATA_VALUE else text[: _MAX_METADATA_VALUE - 1] + "…"


def trace_attributes(
    name: str,
    *,
    context: Any = None,
    session_id: Optional[Any] = None,
    user_id: Optional[Any] = None,
    trigger: Optional[str] = None,
    tags: Iterable[str] = (),
    metadata: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the ``metadata`` dict for a graph invoke config.

    Mixes the reserved ``langfuse_*`` keys (which the LangChain handler lifts
    into trace-level attributes) with plain correlating metadata (which stays on
    the observations). Pass the result as ``config["metadata"]``.

    ``name`` must be a stable, verb-first operation name with no run-specific
    values in it — put those in ``metadata`` instead.
    """
    attributes: Dict[str, Any] = {
        key: _clip(value)
        for key, value in (metadata or {}).items()
        if value is not None and str(value).strip()
    }
    attributes["langfuse_trace_name"] = name

    if session_id:
        attributes["langfuse_session_id"] = str(session_id)
    if user_id:
        attributes["langfuse_user_id"] = str(user_id)

    collected: List[str] = [tag for tag in tags if tag]
    if trigger:
        collected.append(f"trigger:{trigger}")
    if context is not None:
        cluster_id = getattr(context, "cluster_id", None)
        namespace = getattr(context, "namespace", None)
        llm_model = getattr(context, "llm_model", None)
        if cluster_id:
            collected.append(f"cluster:{cluster_id}")
            attributes.setdefault("cluster_id", _clip(cluster_id))
        if namespace:
            attributes.setdefault("namespace", _clip(namespace))
        if llm_model:
            # Not a tag, and never part of the name: swapping models must not
            # break saved filters, and generations already carry the model.
            attributes.setdefault("llm_model", _clip(llm_model))
    if collected:
        attributes["langfuse_tags"] = sorted(set(collected))

    return attributes


# ---------------------------------------------------------------------------
# Invoke-config wiring
# ---------------------------------------------------------------------------


def tracing_callbacks(
    base: Optional[Dict[str, Any]] = None,
    org_langfuse: Optional[Dict[str, Optional[str]]] = None,
) -> Optional[Dict[str, Any]]:
    """Merge the Langfuse handler into an invoke ``config`` dict's callbacks list.

    Returns ``base`` unchanged when tracing is off, so wiring it in is a no-op by
    default. Pass the result straight to ``graph.astream(state, config=...)``.
    """
    handler = get_langfuse_callback(org_langfuse)
    if handler is None:
        return base
    cfg: Dict[str, Any] = dict(base or {})
    callbacks: List[Any] = list(cfg.get("callbacks", []))
    callbacks.append(handler)
    cfg["callbacks"] = callbacks
    return cfg


# ---------------------------------------------------------------------------
# Root observation
# ---------------------------------------------------------------------------


class _RunHandle:
    """What ``trace_run`` yields: a place to record the run's real output."""

    __slots__ = ("_span", "_output")

    def __init__(self, span: Any = None) -> None:
        self._span = span
        self._output: Any = None

    def set_output(self, output: Any) -> None:
        """Record the trace-level output — the thing a reviewer reads first."""
        self._output = output

    def update(self, **kwargs: Any) -> None:
        """Update the root observation (metadata, level, …). No-op when untraced."""
        if self._span is None:
            return
        try:
            self._span.update(**kwargs)
        except Exception as e:  # pragma: no cover - tracing must never break a run
            logger.debug(f"Langfuse root-observation update skipped: {e}")


@contextlib.asynccontextmanager
async def trace_run(
    name: str,
    *,
    org_langfuse: Optional[Dict[str, Optional[str]]] = None,
    input: Any = None,
    metadata: Optional[Mapping[str, Any]] = None,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    tags: Optional[Sequence[str]] = None,
) -> AsyncIterator[_RunHandle]:
    """Wrap one agent run in a named ``agent`` observation with curated I/O.

    Without this, the trace root is LangGraph's own chain run and the
    trace-level input/output is the whole graph state — unreadable in the
    tracing table and in dataset experiments. With it, the root carries the
    alert (or question) and the resulting summary, and every LangGraph span
    nests underneath.

    ``session_id`` / ``user_id`` / ``tags`` are for call sites that answer
    *without* invoking the graph — a direct reply is served from context
    already gathered, so the LangChain handler never runs and nothing else
    would name the trace or file it under the incident's session. Graph call
    sites keep passing these through ``trace_attributes`` instead; setting both
    is harmless (same values, same keys).

    Always yields a handle, traced or not, so call sites need no branch. Flushes
    on exit so a finished run ships even if the process then goes idle.
    """
    client, _ = _resolve_client(org_langfuse)
    if client is None:
        yield _RunHandle()
        return

    span_cm = None
    span = None
    try:
        span_cm = client.start_as_current_observation(
            name=name,
            as_type="agent",
            input=input,
            metadata=dict(metadata or {}) or None,
        )
        span = span_cm.__enter__()
    except Exception as e:  # pragma: no cover - tracing must never break a run
        logger.warning(f"Langfuse root observation unavailable ({e}); running untraced.")
        yield _RunHandle()
        return

    # Entered *inside* the root observation, which is what the SDK requires:
    # it stamps the active span and every span created after this point.
    attributes = {
        key: value
        for key, value in (
            ("trace_name", name),
            ("session_id", session_id),
            ("user_id", user_id),
            ("tags", list(tags) if tags else None),
        )
        if value
    }
    attrs_cm = None
    if attributes:
        try:
            from langfuse import propagate_attributes

            attrs_cm = propagate_attributes(**attributes)
            attrs_cm.__enter__()
        except Exception as e:  # pragma: no cover - a nameless trace beats no run
            logger.debug(f"Langfuse trace attributes skipped: {e}")
            attrs_cm = None

    handle = _RunHandle(span)
    try:
        yield handle
    except BaseException as exc:
        handle.update(level="ERROR", status_message=str(exc)[:500])
        raise
    finally:
        if handle._output is not None:
            handle.update(output=handle._output)
        for closeable in (attrs_cm, span_cm):
            if closeable is None:
                continue
            try:
                closeable.__exit__(None, None, None)
            except Exception as e:  # pragma: no cover
                logger.debug(f"Langfuse observation close skipped: {e}")
        await _flush_async(client)


async def _flush_async(client: Any) -> None:
    """Flush without blocking the event loop (``flush()`` is synchronous)."""
    try:
        import asyncio

        await asyncio.to_thread(client.flush)
    except Exception as e:  # pragma: no cover
        logger.debug(f"Langfuse flush skipped: {e}")


_OBSERVATION_LEVELS = ("DEBUG", "DEFAULT", "WARNING", "ERROR")


def mark_current_observation(level: str, status_message: str) -> None:
    """Flag the active observation so failed work is *findable* in Langfuse.

    A refused or errored tool otherwise exports at ``DEFAULT`` with the failure
    buried inside the output payload. That makes ``level = ERROR`` — the
    standard way to find broken runs, and what alerting and saved views filter
    on — return nothing, so a run where every remediation was refused looks
    exactly like one where they all applied.

    Writes the SDK's own OTel attribute keys on the current span rather than
    going through a client, because the executor is org-agnostic: it has no
    credentials to resolve a client with, and the span it is running inside was
    opened by whichever tenant's client owns this trace.

    *Which* observation gets flagged is worth being precise about. The LangChain
    handler builds its tool/chain observations with ``start_observation`` and
    never attaches them to the OTel context, so the current span here is the
    innermost one the SDK opened *as current* — in practice the ``trace_run``
    root. The flag therefore lands on the run, not on the individual tool
    observation, which is why the message has to name the tool itself. Minting
    our own span instead is not an option: a span created outside a tenant's
    client carries no project scope, and every registered project's span
    processor would export it (``span_processor.on_end``), leaking one org's
    failures into another's Langfuse project.

    Never raises. Never downgrades a level already set on the span — a
    partially-failed step must not be re-labelled healthy by a later success —
    but still records the milder failure's message, and appends rather than
    overwrites, because one call arrives per failed action and a four-refusal
    run that reports only the last refusal is not an honest status line.
    """
    if level not in _OBSERVATION_LEVELS:
        level = "ERROR"
    try:
        from opentelemetry import trace as otel_trace

        from langfuse._client.attributes import LangfuseOtelSpanAttributes as attrs

        span = otel_trace.get_current_span()
        if span is None or not span.is_recording():
            return
        attributes = getattr(span, "attributes", None) or {}
        current = attributes.get(attrs.OBSERVATION_LEVEL)
        if not (
            current in _OBSERVATION_LEVELS
            and _OBSERVATION_LEVELS.index(current) > _OBSERVATION_LEVELS.index(level)
        ):
            span.set_attribute(attrs.OBSERVATION_LEVEL, level)
        message = redact(str(status_message))
        previous = str(attributes.get(attrs.OBSERVATION_STATUS_MESSAGE) or "")
        if previous:
            message = previous if message in previous else f"{previous}; {message}"
        span.set_attribute(attrs.OBSERVATION_STATUS_MESSAGE, message[:1000])
    except Exception as e:  # pragma: no cover - tracing must never break a run
        logger.debug(f"Langfuse observation level not set ({e}).")


def flush(org_langfuse: Optional[Dict[str, Optional[str]]] = None) -> None:
    """Flush pending traces (call on shutdown / after a synchronous run).

    Takes the org credentials for the same reason ``trace_run`` does: with more
    than one project registered, a keyless client lookup returns a disabled
    client and would flush nothing.
    """
    client, _ = _resolve_client(org_langfuse)
    if client is None:
        return
    try:
        client.flush()
    except Exception as e:  # pragma: no cover
        logger.debug(f"Langfuse flush skipped: {e}")


__all__ = [
    "flush",
    "get_langfuse_callback",
    "langfuse_enabled",
    "mark_current_observation",
    "mask_otel_spans",
    "redact",
    "trace_attributes",
    "trace_run",
    "tracing_callbacks",
]
