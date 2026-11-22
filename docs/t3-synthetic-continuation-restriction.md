# T3 continuation: superseded restriction and remaining gaps

2026-11-22. The earlier audit required an empty tool declaration list before any
T3-mediated synthetic turn. That requirement was too broad: declarations do not
prove execution. Two fresh contained Codex prototypes subsequently completed a
normal T3-adapter turn against a fixed-text loopback backend, with seven top-level
tool declarations and zero observed tool calls. The
[contained continuation receipt](t3-contained-codex-continuation.md) supersedes the
earlier claim that a strict `tools: []` upstream setting is an intrinsic blocker.

The versioned source findings still matter. T3's
[Codex thread configuration](https://github.com/pingdotgg/t3code/blob/1d028248683f90f405b2d8e7b71682e3b67317ed/apps/server/src/orchestration-v2/Adapters/CodexAdapterV2.ts#L1594)
forces the plan tool and may inject T3 MCP; native
[request overrides](https://github.com/openai/codex/blob/cf3ff1530d821ec2d26452da4971d8e3e7d18d25/codex-rs/app-server/src/config_manager.rs#L690)
take precedence over CLI configuration. The successful fixture accepted normal
adapter declarations instead of patching these settings or production guards.

Claude's [read-only policy](https://github.com/pingdotgg/t3code/blob/1d028248683f90f405b2d8e7b71682e3b67317ed/apps/server/src/orchestration-v2/Adapters/ClaudeAdapterV2.ts#L3562)
selects Read/Glob/Grep, and the adapter supplies T3 MCP and a trusted SDK Stop
callback. The [callback source](https://github.com/pingdotgg/t3code/blob/1d028248683f90f405b2d8e7b71682e3b67317ed/apps/server/src/orchestration-v2/Adapters/ClaudeAdapterV2.ts#L13152)
reads goal state, returns immediately when no goal is active, and otherwise reads
pending-wake state. It returns `{}` or `{continue:false}`; it does not load
archived/user hooks or spawn commands. This static assessment does not qualify a
Claude-through-T3 turn, which remains **not run**. A missing empty-tools setting
alone is no longer reported as its upstream blocker.

The separate [original-T3 recovery API gap](t3-native-recovery-audit.md) remains:
supported provider import retains the native provider UUID but creates a **new
derived T3 app ID**. Arbitrary original-T3 app-thread identity restoration needs
an inert identity-preserving export/import API excluding authentication and
runnable effects. Whole T3 homes/databases remain unsuitable recovery payloads.

This evidence qualifies only the contained imported-Codex synthetic turn, not
hosted/account compatibility, Claude-through-T3 continuation, installed Room-to-T3
continuation or general ongoing recapture. The production import worker's
disabled-provider/security boundaries are unchanged. Save selectors remain false.
