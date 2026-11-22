# T3 synthetic continuation restriction

2026-11-22. A separate task-native T3 configuration was audited without changing
the production import worker or its disabled-provider boundaries. No supported
strict zero-tools/custom-integrations policy was established, so no T3 provider
turn was attempted. This is a versioned routing/policy restriction, not a hosted
provider or account failure.

Installed T3 reports `0.1.12-nightly.20261122.5098`, ELF SHA256
`93e6bf69ad395da3a2ba9bec2d8bce99579bc9a5c79167735132af9ac7d301ab`.
Its version probe ran with an empty task HOME/T3 home in a new loopback-only
network namespace. No T3 server, authentication session, native provider or
model backend was started. The public research pin is
`1d028248683f90f405b2d8e7b71682e3b67317ed`; build correspondence to that source
is not attested. The installed binary independently contains the forced plan-tool
configuration literal.

## Supported routing versus missing policy

T3's [provider settings](https://github.com/pingdotgg/t3code/blob/1d028248683f90f405b2d8e7b71682e3b67317ed/packages/contracts/src/settings.ts#L717)
expose binary/home paths, launch arguments and custom models. Per-instance
[environment overlays](https://github.com/pingdotgg/t3code/blob/1d028248683f90f405b2d8e7b71682e3b67317ed/packages/provider-core/src/server/instanceEnvironment.ts#L6)
provide a supported route for fresh local endpoint/key settings. An empty Codex
shadow-home setting permits a direct native home without shadow-home links.
Routing alone does not establish that all tools and custom integrations are off.

| Provider | Versioned restriction |
| --- | --- |
| Codex | [T3's thread config](https://github.com/pingdotgg/t3code/blob/1d028248683f90f405b2d8e7b71682e3b67317ed/apps/server/src/orchestration-v2/Adapters/CodexAdapterV2.ts#L1594) forces `tools.update_plan.enabled: true`; its runtime parameters may also inject T3 MCP. The adapter sends those parameters on resume. Native Codex [config precedence](https://github.com/openai/codex/blob/cf3ff1530d821ec2d26452da4971d8e3e7d18d25/codex-rs/app-server/src/config_manager.rs#L690) applies per-request overrides after CLI overrides, so native config/launch arguments cannot undo the later forced plan tool. |
| Claude | The [read-only policy](https://github.com/pingdotgg/t3code/blob/1d028248683f90f405b2d8e7b71682e3b67317ed/apps/server/src/orchestration-v2/Adapters/ClaudeAdapterV2.ts#L3562) supplies a fixed Read/Glob/Grep subset; other policies select preset tools. The adapter can inject T3 MCP and supplies an SDK Stop hook. Native `--bare`, `--safe-mode` and empty-tools flags do not themselves prove precedence over explicit SDK-supplied tools/MCP/hooks. No supported strict-disable property or verified override precedence was established. |

The [runtime-policy contract](https://github.com/pingdotgg/t3code/blob/1d028248683f90f405b2d8e7b71682e3b67317ed/packages/provider-core/src/server/ProviderAdapter.ts#L64)
does not expose a tools/MCP/custom-hooks disable policy. No unsupported duplicate
flag behavior, instrumentation or security relaxation was used to evade this.

## Minimal upstream requirement and current mode

Expose an explicit per-instance/thread strict no-tools/custom-integrations policy.
It must omit the forced Codex plan tool and T3 MCP registrations, pass Claude an
empty tools/MCP configuration without custom hooks, reject conflicting settings,
and verify that native requests declare `tools: []` before a successful test turn.

This is separate from the missing original-T3 identity-preserving inert recovery
API. The existing qualified mode remains explicit imported history with original
provider UUIDs and **new derived T3 app IDs**. See the
[owned-server import receipt](t3-owned-import-acceptance.md). T3-mediated
continuation remains unqualified; original-T3 app-thread restoration remains
unsupported. Save selectors remain false. No production source was changed.
