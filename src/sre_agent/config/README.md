# Agent configuration

`agent_config.yaml` declares specialist roles and their allowed tools.
`prompts/` contains versioned runtime instructions loaded by
`sre_agent.prompt_loader`.

Tool access should be narrowed here and at the MCP server boundary. Prompt
changes are protected by the release-evidence policy under
`evals/benchmarks/release/`; changing a prompt without evidence matching the
new source digest blocks promotion.

Add a specialist by updating its declarative tool set, prompt, graph wiring,
bounded execution tests, and evaluation coverage together.
