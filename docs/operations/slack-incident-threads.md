# Slack incident threads

Slack integration is optional. When an organization has a verified bot token,
Sentinel can open one thread per incident, persist the incident-to-thread
mapping, stream selected timeline events, and route human replies back through
the same incident workflow.

## Supported interactions

- Mention the app for a natural-language metrics query or incident follow-up.
- Reply in a tracked incident thread without repeating the mention.
- Approve or deny a remediation gate.
- Approve a proposed fix, acknowledge a verified fix, or mark a human-resolved
  incident closed.

Every privileged command resolves the Slack user's profile email to a platform
user in the incident's organization. Missing identity or insufficient role
fails closed. Slack does not create a second remediation path: decisions reuse
the same persisted gates and mutation boundary as the API.

## Runtime components

- [`integrations/slack_bot.py`](../../src/sre_agent/integrations/slack_bot.py)
  wires Slack Bolt events and Socket Mode.
- [`war_room.py`](../../src/sre_agent/war_room.py) parses commands, maps threads
  to incidents, and routes replies.
- [`war_room_service.py`](../../src/sre_agent/war_room_service.py) opens threads,
  persists mappings, and forwards incident events.
- [`nl_query.py`](../../src/sre_agent/nl_query.py) validates bounded PromQL
  templates before execution.

## Configuration

An administrator can store a verified organization bot token through the Team
or connection settings. Socket Mode also requires a process-level
`SLACK_APP_TOKEN`, because one running process owns the workspace connection.
The bot needs only the scopes used to read mentions/profile email and post
thread replies.

For a direct development run:

```bash
export SLACK_BOT_TOKEN=xoxb-...
export SLACK_APP_TOKEN=xapp-...
uv run python -m sre_agent.integrations.slack_bot
```

Do not place real Slack tokens in documentation, fixtures, screenshots, or
terminal captures. The bot is optional and failures to post must not change the
incident's authoritative state.
