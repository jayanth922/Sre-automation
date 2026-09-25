# GitHub-exec MCP Server

The **write** counterpart to the read-only github MCP server — the "hands" for
**code-change remediation**. This is what makes the loop "LLM-suggested code
change → executed → verified" real: when the Planner's root cause is a bad
deploy, the ACT executor routes a `revert_commit` action here to open a revert PR.

Port **4006**. Tools:

| Tool | Effect |
| --- | --- |
| `github_exec_health` | Connectivity + safety envelope |
| `create_revert_pr` | Open a PR reverting a bad commit/PR |
| `comment_on_pr` | Post the agent's rationale on a PR |

## Safety

- **Dry-run by default** — returns the exact operation it would perform.
- **Guardrails** (`guardrails.py`): allow-list of actions (`create_revert_pr`,
  `comment_on_pr` only — never force-push / delete / close arbitrary PRs) and an
  allow-list of repos (`GITHUB_EXEC_ALLOWED_REPOS`, default `GITHUB_REPO`).
- A revert PR is safe by construction: it *proposes* an undo a human/CI merges.
- Least-privilege GitHub auth (`GITHUB_TOKEN`, ideally a scoped GitHub App).

## Config

`GITHUB_REPO`, `GITHUB_TOKEN`, and `GITHUB_EXEC_ALLOWED_REPOS` (required for live
writes — empty allow-list refuses mutations). Optional
`GITHUB_EXEC_REVERT_LABEL` triggers an external CI workflow and returns
`WORKFLOW_TRIGGERED` (not `CREATED`). Live `create_revert_pr` returns:

| Status | Meaning |
| --- | --- |
| `CREATED` | A revert PR was opened (`pr_url` set, `applied=true`) |
| `WORKFLOW_TRIGGERED` | Label applied to trigger CI; no PR created by this tool |
| `MANUAL_REQUIRED` | No PR created; `plan` has operator steps |
| `REFUSED` / `ERROR` | Guardrail or tooling failure |

The agent discovers it via `MCP_GITHUB_EXEC_URI` (e.g. `http://localhost:4006/sse`).
