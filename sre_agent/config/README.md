# sre_agent config

This directory is the declarative brain-shaping layer for the agent. It decides which specialists exist, which tools they can touch, and which instruction templates they should receive at runtime.

The key benefit of keeping these decisions here is that the runtime code stays focused on orchestration while this folder remains the human-readable declaration of the assistant’s behavior.

## Contents

- [agent_config.yaml](agent_config.yaml) maps specialist roles to tool names.
- [prompts/](prompts/) contains the prompt catalog used by the supervisor and specialist agents.

## Configuration Model

The runtime reads the YAML registry, builds the specialist agent instances, and then filters the available tools so each role sees only the capabilities it is supposed to use. That keeps the graph from turning into a single undifferentiated tool bucket.

The prompt loader uses the files in this folder to construct the working instructions for:

- the base agent prompt,
- the Kubernetes, logs, metrics, runbooks, and GitHub specialists,
- the supervisor’s planning and aggregation passes,
- and the executive-summary output flow.

## The Agent Registry

[agent_config.yaml](agent_config.yaml) is the first place to look when you want to understand the capability model. It explains which tools are available to each specialist role and therefore what kind of evidence that specialist can produce.

That file is especially important when adding a new tool server or when you want to narrow a specialist’s power for safety or clarity.

## How To Extend

To add a new specialist:

1. Add the role and tool list to [agent_config.yaml](agent_config.yaml).
2. Add or update the relevant prompt template in [prompts/](prompts/).
3. Wire the new agent into the graph and tool wrapper code under [../](../).
4. Update the documentation here so the new capability is discoverable.

Keep the configuration declarative. The runtime should read this directory rather than hard-coding role/tool maps in multiple places.

## Why This Matters

The assistant’s behavior is not only determined by code. A large part of its shape comes from which tools the specialists can use and what the prompts ask them to produce. This folder is therefore part configuration, part behavior contract.

## Related Docs

- [prompts/README.md](prompts/README.md)
- [../README.md](../README.md)
- [../../backend/README.md](../../backend/README.md)