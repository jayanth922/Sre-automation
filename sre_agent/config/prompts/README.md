# Prompt Catalog

This folder contains the prompt templates that shape the behavior of the SRE agent. The files are intentionally separated by role so the runtime can compose the right instructions for the right part of the graph without mixing specialist concerns together.

The prompt structure is one of the most important parts of the assistant because it defines what counts as a good answer, what evidence should be gathered, and how the supervisor should summarize incomplete or conflicting results.

## Prompt Groups

### Base And Specialist Prompts

- [agent_base_prompt.txt](agent_base_prompt.txt) is the common baseline used across the system.
- [kubernetes_agent_prompt.txt](kubernetes_agent_prompt.txt) defines the infrastructure specialist behavior.
- [logs_agent_prompt.txt](logs_agent_prompt.txt) defines the log-analysis specialist behavior.
- [metrics_agent_prompt.txt](metrics_agent_prompt.txt) defines the metrics specialist behavior.
- [runbooks_agent_prompt.txt](runbooks_agent_prompt.txt) defines the operational runbooks specialist behavior.
- [github_agent_prompt.txt](github_agent_prompt.txt) defines the code-change correlation specialist behavior.

### Supervisor Prompts

- [supervisor_planning_prompt.txt](supervisor_planning_prompt.txt) drives the planning step.
- [supervisor_multi_agent_prompt.txt](supervisor_multi_agent_prompt.txt) coordinates multi-agent routing.
- [supervisor_fallback_prompt.txt](supervisor_fallback_prompt.txt) provides the fallback path when evidence is incomplete.
- [supervisor_plan_aggregation.txt](supervisor_plan_aggregation.txt) aggregates planning output.
- [supervisor_standard_aggregation.txt](supervisor_standard_aggregation.txt) builds the normal response summary.

### Summary And User Templates

- [executive_summary_system.txt](executive_summary_system.txt) defines the system-level summary framing.
- [executive_summary_user_template.txt](executive_summary_user_template.txt) defines the user-facing summary template.
- [data_pattern_guide.txt](data_pattern_guide.txt) helps normalize structured evidence.

## How These Files Are Used

The prompt loader assembles these templates into the active conversation context for the graph nodes. Specialist prompts keep the evidence-gathering roles narrow, while the supervisor templates decide how to sequence agents and how to summarize their results.

In practice, these files control things like:

- how the specialist should format findings,
- what level of evidence is considered acceptable,
- how the supervisor should respond when a tool is unavailable,
- how the final summary should read for the human operator,
- and how follow-up questions should be phrased when the investigation is not yet complete.

## Working Rules

- Keep prompts explicit about output shape and evidence quality.
- Prefer small, focused templates over large monolithic instructions.
- Keep specialist prompts narrow and role-specific.
- Make supervisor prompts resilient to partial data and tool failure.
- Update this README whenever new prompt files are added so the catalog stays current.

## Why This Folder Matters

The behavior of the assistant changes dramatically based on prompt content even when the code stays the same. This folder is therefore part of the product contract, not just an implementation detail.

## Related Docs

- [../README.md](../README.md)
- [../../README.md](../../README.md)