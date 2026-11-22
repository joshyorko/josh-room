# Synthetic native continuation acceptance

Verified 2026-11-22 on Linux. Installed Codex and Claude resumed an original
provider session after encrypted capture and cold native-file materialization,
then completed one turn against task-private deterministic loopback backends.
These backends return fixed protocol messages. They perform no model inference.
This proves native runtime loading, restored-context transmission, original-ID
continuation and native persistence for these fixtures. It does not prove hosted
provider/account compatibility, answer quality or production availability.

## Exact scope

| Boundary | Observed result |
| --- | --- |
| Codex | CLI `0.171.0`, Linux binary SHA256 `b76a027d6b210f9a8da7aaf321dd235c700f0ef7a794d4a5314b9af6c134f0f0`. Actual stdio `thread/resume`, `turn/start`, completed-turn notification and `thread/read`. |
| Claude | CLI `2.1.310`, Linux binary SHA256 `b73676430af12f97316a798b8371f81d640f90a58bdcb8f3ce56a835bb47df56`. Actual `--resume` plus one `--print` turn, original `session_id`, `num_turns: 1`. |
| Native identity | Original provider UUID `11111111-1111-4111-8111-111111111111` and canonical cwd preserved. Both source profiles were deleted before runtime startup. Restored original JSONL prefixes stayed byte-identical; native runtimes appended the new prompt and fixed assistant reply. |
| Backend requests | Exactly one authenticated Responses POST for Codex and one authenticated Messages POST for Claude. Both contained the old user/assistant history and new prompt, and declared `tools: []`. Unexpected credentials, model names, paths or tools fail the backend test. |
| Authentication | Fresh generated disposable API keys passed only through supported native environment settings. Empty task-private HOME/config roots; no existing provider/user auth, keychain, host socket or credential copy. Neither native home retained the disposable key or created an auth store. |
| Network | Fresh `unshare -Urn` namespace, distinct from the launcher namespace, only `lo`, empty IPv4 routes, no usable IPv6 default or external-interface route. Checks run before native invocation and again afterward. Backends bind only `127.0.0.1`. |
| Capture/restore | Real age `1.3.2`, native file capture and supported one-file materializer on disk-backed task storage. Host admission is mocked synthetic authority. This is separate from earlier real MinIO/Secret Service Room-reference lifecycle acceptance. |
| Combined final native test | PASS, 2 passed in 2.14s. Default opt-out: 2 skipped in 0.08s. Both explicit host-namespace attempts failed at the namespace guard before fixture acquisition or native invocation. |
| Independent review | SCOPED PASS, static source/security/comment and whitespace review only. No independent runtime rerun or syscall/filesystem-read audit. |

The public source pin for Codex research remains
`cf3ff1530d821ec2d26452da4971d8e3e7d18d25`; it is not an attested build SHA for
the installed executable. The binary's own generated protocol schemas confirmed
the continuation methods and fields. Public-source retrieval was separate from
the native acceptance namespace; no external provider request was made.

## Supported native settings

Codex uses a custom `model_providers.offline` entry with loopback `base_url`,
`wire_api = "responses"`, `env_key`, `requires_openai_auth = false`, no retries
and no WebSocket transport. This is the supported custom-provider authentication
contract, not a bypass of OpenAI account restrictions. `cli_auth_credentials_store`
is `file` in the empty dedicated `CODEX_HOME`; no `auth.json` is restored or created.

The supported `model_catalog_json` setting supplies a minimal synthetic entry
with disabled shell and patch tools and no experimental tools. Supported config
switches additionally disable tools, hooks, plugins, MCP configuration, agents,
web search, memory, telemetry and model discovery. `--strict-config` remains on;
the sandbox is read-only. The local backend refuses any nonempty tool list.
No production model instructions/catalog entries are copied.

Pinned source contracts:
[provider/config schema](https://github.com/openai/codex/blob/cf3ff1530d821ec2d26452da4971d8e3e7d18d25/codex-rs/core/config.schema.json),
[model metadata](https://github.com/openai/codex/blob/cf3ff1530d821ec2d26452da4971d8e3e7d18d25/codex-rs/protocol/src/openai_models.rs),
[tool registration](https://github.com/openai/codex/blob/cf3ff1530d821ec2d26452da4971d8e3e7d18d25/codex-rs/core/src/tools/spec_plan.rs),
[upstream synthetic SSE examples](https://github.com/openai/codex/blob/cf3ff1530d821ec2d26452da4971d8e3e7d18d25/codex-rs/core/tests/common/responses.rs).

Claude uses the supported `ANTHROPIC_BASE_URL` and `ANTHROPIC_API_KEY` settings,
with `CLAUDE_CONFIG_DIR` pointing at the fresh restored native directory.
Installed `2.1.310` help documents `--bare` as API-key-only startup without
OAuth/keychain reads, hooks, plugin sync, memory/prefetch or CLAUDE.md loading.
The test also uses `--safe-mode`, an empty `--tools` list, strict empty MCP
configuration, empty setting sources, disabled slash commands/Chrome and
`dontAsk` permission mode. It never uses a dangerous permission bypass.
The [versioned Claude audit](claude-native-recovery-audit.md) records native-file
provenance and the excluded SessionStore-backed credential-copy route.

## Reproduce on the Linux host

Use the repo Python environment, installed exact binaries, age/age-keygen,
`unshare` and `ip`. Check free space first. Use disk-backed task storage rather
than a tmpfs for the materializer. Run no native acceptance on the ordinary host
network; explicit opt-in outside the namespace deliberately fails.

```sh
TMPDIR=/var/tmp
TASK_ROOT=$(mktemp -d "$TMPDIR/codex-native-continuation-XXXXXX")
mkdir -m 700 "$TASK_ROOT/home"
NATIVE_OUTER_NETNS=$(readlink /proc/self/ns/net)
unshare -Urn -- sh -c '
  ip link set lo up
  exec env -i PATH="$1" HOME="$2" \
    JOSH_ROOM_TEST_NATIVE_OUTER_NETNS="$3" \
    JOSH_ROOM_TEST_CODEX_CLI="$4" JOSH_ROOM_TEST_CLAUDE_CLI="$5" \
    JOSH_ROOM_TEST_CODEX_CONTINUATION=1 JOSH_ROOM_TEST_CLAUDE_CONTINUATION=1 \
    .venv/bin/python -B -m pytest -p no:cacheprovider \
    tests/test_native_codex_continuation.py \
    tests/test_native_claude_continuation.py -q -s --basetemp="$6"
' sh "$PATH" "$TASK_ROOT/home" "$NATIVE_OUTER_NETNS" \
  "$(command -v codex)" "$(command -v claude)" "$TASK_ROOT/tests"
```

The tests stop their owned native processes and HTTP servers. After recording
sanitized results, delete only the task directory and any disposable Python
environment created for the trial. Never retain raw identities, native keyring
data, generated credentials or task-private runtime logs in Git.

## Remaining gates

No production capability or Save selector changes follow from this test.
`native_resume_verified: false` remains the general capability gate. The Room
commands still expose history-only recovery. The test does not run the combined
durable Room-reference/installed-command/new-turn chain or qualify a new immutable
controller artifact or Windows.

The subsequent [combined installed receipt](native-agent-installed-chain.md)
closes the durable Room-reference/registered-installed-restore/native-turn split
for this same synthetic backend scope. It retains the source-runtime override
and does not qualify immutable RCC, hosted accounts or Save integration.

Claude appends runtime rows beyond the closed text-history capture allowlist.
The test explicitly proves that recapturing that richer file remains refused;
it does not widen the policy. Repeated captures/turns and broader native formats
need separate qualification. Claude also emits a hypothetical dollar estimate
and a `firstParty` provider label for this local fixture. Those CLI fields do not
establish hosted inference or billing; the only request went to the fixed local
Messages backend.

T3's earlier supported scan/import still preserves provider UUIDs while deriving
**new T3 app IDs**. No T3 turn was run here. Original-T3 app identity restoration
remains unsupported, and T3-mediated continuation remains unqualified. See the
[owned-server import receipt](t3-owned-import-acceptance.md). Hosted/paid calls,
existing user/provider authentication, release actions and Save integration
remain outside this acceptance.
