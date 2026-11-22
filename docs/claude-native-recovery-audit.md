# Claude native recovery audit

Audit date: 2026-11-22. Josh Room base
`5ac1904901b57169a2796d55dd67b00bf7378509`. This audit uses synthetic local
history and does not establish native runtime continuation or a new turn.

## Version and provenance

| Artifact | Observed identity |
| --- | --- |
| Installed Linux CLI | Claude Code `2.1.310`, ELF x86_64 |
| Official SDK inspected and exercised | `@anthropic-ai/claude-agent-sdk@0.3.310` |
| SDK native manifest | CLI `2.1.310`, commit `0d2056ecf42d9452a8cd8e054600024e7c557add` |
| Installed CLI SHA256 | `b73676430af12f97316a798b8371f81d640f90a58bdcb8f3ce56a835bb47df56`; matches the SDK manifest's Linux x64 binary digest |
| SDK tarball SHA256 | `110e2584420b1feff99694efafc58fdb2f35d7a0c5230abe152c1d82fee0384c` |

The official [SDK distribution](https://registry.npmjs.org/@anthropic-ai/claude-agent-sdk/-/claude-agent-sdk-0.3.310.tgz)
contains public type declarations and the bundled reader implementation. Its
license reserves Anthropic's rights and refers to the vendor's
[legal agreements](https://code.claude.com/docs/en/legal-and-compliance).
Josh Room must not vendor the SDK source or redistribute its binaries as part of
this change. Invoking a separately installed SDK for acceptance is distinct from
shipping that SDK.

## Supported local recovery contract

The vendor [cross-host session guidance](https://code.claude.com/docs/en/agent-sdk/sessions#resume-across-hosts)
explicitly supports moving the native session JSONL file between hosts before
resume. The canonical filesystem layout is the selected Claude config root's
`projects/<encoded-cwd>/<session-uuid>.jsonl`. Josh Room still requires the original
canonical workspace path; the vendor's broader ID search does not authorize
relocation or cross-Room capture.

The SDK's public, filesystem-only readers are `listSessions`, `getSessionInfo`,
and `getSessionMessages`. Listing accepts an explicit `dir` and
`includeWorktrees: false`. Message reading resolves the native conversation by
`parentUuid` and returns original session/message identities. Default listing can
span projects/worktrees; it must never substitute for Josh Room admission.

For the qualified short ASCII workspace path, the project key replaces each
non-alphanumeric character with a dash. Keys longer than 200 characters use an
additional implementation-specific hash suffix. `CLAUDE_CODE_PROJECT_DIR_NAME`
can override the key when used beside `CLAUDE_CONFIG_DIR`. Long paths, overrides,
Unicode normalization, Windows and path collisions need separate qualification;
a first policy can reject them rather than invent portability.

## Observed offline discovery

PASS: SDK `0.3.310` listed a synthetic native main session by its original UUID,
returned its cwd and first prompt, and read its two original user/assistant message
UUIDs and text. Listing another cwd returned no session. The fixture had explicit
`version: "2.1.310"`, canonical cwd, sessionId, timestamps, UUIDs, a null root parent,
and an assistant parent pointing to the user UUID. No source identity was rewritten.

The process used empty isolated HOME and CLAUDE_CONFIG_DIR with credentials absent.
Before SDK import, the probe prohibited fetch, HTTP/HTTPS, net/TLS sockets, and
subprocess spawn/exec/fork APIs. Its recorded call list remained empty. No query,
resume, prewarm, authentication, or model method ran. This qualifies native reader
acceptance only; the installed CLI runtime was inspected with help/version rather
than asked to execute a turn.

## Narrow safe slice and excluded state

A closed first history policy can preserve one unchanged main-session JSONL with
linear, complete user/assistant text history. It must validate exact native path,
cwd, version, session/message UUIDs and parent chain; reject missing parents,
cycles, branches, duplicate identities, partial assistants and dependency-bearing
messages; retain source bytes; and scan content under existing material policy.
The runtime may accept more formats, but that is not evidence those formats are
safe or complete for this recovery unit.

Exclude sidechains, subagents, tool uses/results, attachments, compaction,
file-checkpoint backups, pending queue operations and unknown record shapes until
qualified. Do not capture root `history.jsonl`, `.credentials.json`, `.claude.json`,
settings, plugins, MCP state, keychains or a whole config/home directory. Session
text can itself contain secrets; path exclusions are not DLP.

## Unsafe store-resume route and remaining gate

The public [session-store behavior](https://code.claude.com/docs/en/agent-sdk/session-storage#resume-from-the-store)
and SDK `0.3.310` implementation show that store-backed resume creates a named
temporary config directory and seeds it with authentication/configuration from
the original config root. It can also consult the macOS keychain. That route
violates Josh Room's no-credential-copy and interrupted-plaintext-staging
boundaries. `importSessionToStore` exports local entries to a store; it is not a
safe original-ID local import replacement. `forkSession` deliberately changes the
session ID and cannot count as restoration of the original session.

The existing anonymous-inode single-file materializer can avoid that store path
for qualified JSONL history. Fresh authentication must be established separately
through supported native means before runtime resume is attempted. Original-ID
runtime acceptance and an observed new turn remain unverified. No user-only login,
credential, or paid-call prerequisite is needed for this offline history slice;
such prerequisites belong only to a later explicitly approved continuation test.

No existing profile/auth store was opened. No hosted/model/paid call, publication,
or service mutation was performed. SDK/package/probe artifacts remain only in the
parent-owned disk task directory for its cold-pipeline acceptance and cleanup.
