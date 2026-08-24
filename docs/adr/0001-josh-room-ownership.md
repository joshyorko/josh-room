# ADR 0001 — Josh Room ownership boundaries

Josh Room owns logical projects, encrypted snapshot catalog resolution, the
snapshot envelope, encryption orchestration, transport adapters, safe hydration,
receipts, doctor, and the CLI/VS Code entry point. Room of Requirement owns the
image substrate; Josh All The Things owns folder snapshot capture and restore;
age owns encryption; R2 owns durable production blobs; RCC owns environments;
Actions and Hive remain future integration consumers.

Josh Room does not become an agent harness, package runtime, container provider,
source-control manager, sync daemon, or multi-user platform.
