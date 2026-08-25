# ADR 0007 — Deferred RCC, Actions, Hive, and OpenAI projections

RCC remains the owner of environment specifications, artifacts, providers,
materialization, and execution; Josh Room consumes the immutable JAT RCC
v18.19.2 Environment Artifact at bootstrap and carries optional typed receipt
metadata when Save requests `rcc_environment=auto`. Actions remains the owner of packages,
deployments, runs, workers, work items, and outputs. Hive remains the owner of
agent backends, models, tools, connections, and task orchestration. A future
bounded projection may consume a Josh Room snapshot, but this MVP adds no
provider, SDK, MCP server, upload, or agent harness.
