# Chat Integration (project #3: Slack + AI / Buzz / PromptQL)

The video's #3 is the "AI-native workspace" idea: in Slack — or Jack Dorsey's
open-source **Buzz** — you don't just have human members, you have **agent
members** you can tag, and they go do their thing. Paired with **PromptQL**
(Hasura's plan-execute-verify NL→data agent), the SRE application is:

> On-call tags the SRE agent in chat and either **asks a data question** in plain
> English (→ a *verified* PromQL query) or **steers the live investigation**
> (→ the existing human-checkpoint queue).

## What's built (the substance)

- **NL → verified query** (`sre_agent/nl_query.py`) — the PromptQL pattern:
  `parse intent → generate PromQL → validate → execute`. The **validate** step is
  the guarantee: only allow-listed metrics/functions, bounded time windows, and
  balanced syntax are ever executed (no NL-to-query hallucination reaching prod).
  Deterministic templates cover error rate, latency, traffic, saturation and
  payment failures; execution goes through the Prometheus MCP tool.
- **Chat routing** (`classify_chat_message`) — decides whether a message is a
  data **query**, an investigation **steer**, or a **greeting**.
- **Steer bridge** (`build_incident_message_payload`) — shapes a steer into the
  existing `POST /api/v1/incidents/{id}/message` endpoint, which already feeds the
  supervisor's human-checkpoint interrupt queue. So chat steering reuses the
  interrupt mechanism the platform already has.

## The thin transport layer (to wire per deployment)

Only the chat transport is deployment-specific; the reasoning above is done:

1. **Buzz** (`github.com/block/buzz`, Rust, open source) — register the SRE agent
   as a Buzz agent member. Buzz delivers each tagged message as a signed event;
   forward the text to `classify_chat_message` and dispatch:
   - `query` → `run_nl_query(text, prometheus_tool_caller)` → post the result.
   - `steer` → `build_incident_message_payload` → POST to the incident.
   - `greeting` → acknowledge.
   Buzz's signed, hash-chained event log maps onto the platform's `AuditLog`.
2. **Slack** — a Slack app (OAuth, scoped bot token) subscribed to `app_mention`
   via the Events API; same dispatch. Least-privilege: request only the scopes
   needed (`app_mentions:read`, `chat:write`).

## Security

Same posture as the rest of the platform: least-privilege OAuth scopes, secrets
in the customer boundary, and (for Buzz) per-agent cryptographic identity. See
`docs/ACT_PHASE_DESIGN.md` §5 for the egress-only / least-privilege model.
