# Trace and model-accounting evidence

A08 creates one root incident-run trace and requires child spans for model,
retrieval, tool, policy, approval, mutation, and verification activity. The
metadata-only root trace is appended to `TRACE_EVIDENCE_PATH` (default
`reports/run-trace.jsonl`) with OpenTelemetry GenAI-compatible operation,
provider, model, usage, workflow, conversation, and tool attributes. Every
record carries the A01 root trace and the available manifest, incident, job,
call, and parent identifiers.

`ModelAccountingCallback` is attached to every model built by `route_llm`.
Each call appends requested and observed routing, fallback evidence,
provider-reported token usage and cost, latency, and error type to
`MODEL_ACCOUNTING_PATH` (default `reports/model-accounting.jsonl`). Missing
provider cost remains unknown; no mutable or guessed price table is used.

Prompt, model-output, retrieval-query, tool-payload, exception-text, credential,
and tenant data are absent by default. `TRACE_PAYLOAD_CAPTURE=true` enables
redacted, length-limited diagnostic payloads; `TRACE_PAYLOAD_MAX_CHARS` is
clamped to 64–10000 characters. Both artifacts are written with mode `0600`.

A trace summary is complete only after successful root finalization, all seven
required child-span kinds and attributes, durable artifact writes, and complete
model accounting. Error spans remain counted so handled failures reconcile
without disappearing. Cost, tokens, and model latency are exposed only for a
complete trace. Job results and the incident `agent-metrics` API expose both
the model-accounting detail and root-trace summary.

Paired trial v2 pins the root-trace artifact path, record-set SHA-256, and span
count. Promotion blocks when any baseline or candidate trial lacks complete
trace evidence or provider-reported cost.
