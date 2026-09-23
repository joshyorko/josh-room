# Capture policy v1

Josh Room capture authorization is host-owned. A policy is read only from the
private XDG file `$XDG_CONFIG_HOME/josh-room/policy.json` (or
`~/.config/josh-room/policy.json` when `XDG_CONFIG_HOME` is unset). The public
shape is [capture-policy-v1.schema.json](../schemas/capture-policy-v1.schema.json);
the checked-in example contains only synthetic identities.

The policy boundary is intentionally separate from the evidence envelope and
source adapters. It names logical sources such as `codex.transcript` and
`codex.session-metadata`, but it does not define records, checkpoints, material
classification, encryption, or transport.

## Decision precedence

The pure decision function applies these rules in order:

1. Untrusted context, malformed remotes, unsafe paths, and symlinked workspaces
   fail closed to `local-only` or `quarantine`.
2. An interactive, confirmed operator CLI override may select one named profile.
   Hooks and non-interactive callers cannot use this authority increase.
3. A matching explicit deny wins over every allow rule.
4. Exactly one host-observed allow rule must match the sanitized repository and
   stable workspace ID. No match is `local-only`; ambiguity is `quarantine`.
5. Source names, triggers, capture mode, and byte limits are checked.
6. A `local-only` destination returns `local-only`; a named private R2 binding
   returns `allow` only when its configured scope equals the profile name.

For PCC replay, a private-R2 policy binding ID is the exact host-configured
Dimension ID. The CLI cannot redirect replay to another Dimension that happens
to reuse the same credential profile.

The decision and dry-run output contain logical source names, bounded size
estimates, profile metadata, destination class, policy version, provenance, and
stable reason codes. They do not contain transcript text, secrets, absolute
paths, recipient references, credential profiles, buckets, or endpoints.

## Authority boundary

Repository files, transcript text, model output, hook payloads, inherited
repository environment, and R2 objects are data, not authority. They are not
read by the policy loader and cannot choose a profile, destination, capture
mode, recipients, or limits. A hook payload is represented as untrusted context
and receives `local-only` even when it claims an allowed repository. A host
controller may pass a sanitized, host-observed repository identity; matching it
against a private allow rule is not the same as accepting a repository-provided
profile or destination claim.

Git remotes are reduced to lower-case host, namespace, and repository name.
HTTPS credentials, password-bearing SSH URLs, and non-`git` scp-style users are
rejected before matching. Paths are normalized lexically for diagnostics only;
policy matching never uses raw paths, display names, worktree locations, or
symlink targets.

The four profiles are independent policy namespaces: `personal`, `work`, `oss`,
and `homelab`. A private R2 binding carries one scope, and a profile can use
only a binding with the same scope. The example therefore cannot resolve
`work` to a personal R2 binding. Work is local-only until a separately scoped
work binding is configured.

Retention is a limit supplied to the local outbox/R2 lifecycle consumer. This
module does not delete snapshots or implement garbage collection.

## Deployment-shaped examples

The same synthetic schema supports these host configurations without inferring
authority from the environment:

- personal laptop: an explicitly mapped `workspace-personal` may use the
  separately scoped `personal-r2` binding;
- work workstation: `workspace-work` is local-only by default;
- remote VM: an unknown or untrusted context remains local-only;
- devcontainer: a host-observed stable workspace/repository mapping is required;
- public OSS Codespace: only an explicitly mapped OSS repository may use the
  separately scoped `oss-r2` binding.

No profile mapping, bucket, employer, customer, private path, identity, or
credential belongs in a public fixture.
