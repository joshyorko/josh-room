# Capture policy v1

The public contract is [`schemas/capture-policy-v1.schema.json`](../schemas/capture-policy-v1.schema.json), identified by its required `$schema` value. A private host policy contains exactly the `personal`, `work`, `oss`, and `homelab` profiles. Repository files, transcripts, hook payloads, model output, inherited repository environment, and R2 cannot select a profile or destination.

Private policy JSON is loaded only from `$XDG_CONFIG_HOME/josh-room/policy.json` (or the host `~/.config` default). The config directory and policy file must be host-owned; the policy directory and file must not be broadly accessible, and symlinked paths fail closed. Malformed policy parsing returns a deny decision with a stable `policy.*` reason code.

Policy limits are positive integers. The private loader rejects decimal JSON number tokens such as `1.0`, even where a general JSON Schema implementation treats an integral numeric value as an `integer`. Capture request size observations are integer values; invalid non-integer values are rejected before decision or dry-run serialization.

Logical sources are bounded to at most 32 unique `codex.*` names, each at most 96 characters. Secret-like names containing markers such as `token`, `credential`, or `secret` are rejected without echoing the input.

Repository identities use a lower-case host of at most 253 characters, a slash-separated namespace of at most 128 characters, and a name of at most 128 characters. Namespace and name components are bounded to 128 characters and `.`/`..` path segments are rejected before identity normalization. Credential-bearing remotes are rejected.
