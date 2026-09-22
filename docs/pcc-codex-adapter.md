# Codex transcript source adapter

Issue #6 adds the first production filesystem-backed logical source adapter:
`codex.transcript`. It consumes only explicitly configured active and archived
Codex session roots and uses the Wave-1 `LogicalSourceAdapter`/
`Checkpoint` contract from issue #5.

## Upstream behavior pinned for this adapter

The implementation was checked against Codex upstream commit
`a1f40f3f1326eff7c81b860a4f4e27c1be186e10`, dated `2026-09-22`:

- lifecycle hook facts include bounded `session_id`, `cwd`, and
  `transcript_path` fields;
- rollout files are append-oriented JSONL sources;
- active plain JSONL can move to archived plain JSONL and then to archived
  `.jsonl.zst`, which this adapter reads as a streaming representation.

The adapter does not install hooks or depend on the app-server. Hook facts are
optional input; when absent, resolution scans only the two explicit roots with
file, byte, depth, and time limits. SQLite, indexes, configuration, auth,
keyrings, logs, plugin state, caches, and unrelated files are outside the
source boundary.

## Record policy

The allowlist handles session metadata, visible user/assistant messages,
explicit tool-call/result metadata, usage metadata, approvals, and sanitized
repository metadata. It excludes reasoning/encrypted reasoning, auth and
credential records, and unknown record kinds. Unknown kinds or unknown schema
majors quarantine the source; they are never copied wholesale. Large values
are represented only by bounded size/digest metadata, and oversized lines are
bounded before decompression output can become an in-memory record.

Diagnostics and contract JSON contain adapter, logical source, representation,
session, checkpoint, and stable status identifiers only. Canonical host paths,
transcript text, credentials, and raw provider values never enter those
outputs.
