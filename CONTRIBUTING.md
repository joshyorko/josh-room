# Contributing and pull request evidence

Use this guide for every pull request. It defines Josh Room's project policy
for documentation and verification evidence.

## State the documentation impact

Every pull request must say which canonical documentation it updates, or give
a specific reason that the change has no documentation impact.

Update the canonical docs when a change alters behavior, commands, dependencies,
architecture, supported platforms, operations, or known limits. Read the docs
that describe the affected user or maintainer task before editing them. Update
those docs in the same pull request as the change.

If none of those contracts change, write a concrete reason. For example:

> No documentation impact. This changes an internal variable name without
> changing behavior, commands, dependencies, supported platforms, or operations.

Do not add empty or unrelated edits to make the documentation section look
complete. Do not use “N/A” or “docs unchanged” without saying why.

## Report exact verification evidence

For each check, name the command or workflow, the scope it covered, and its
observed result. Use `PASS`, `FAIL`, `SKIPPED`, or `NOT RUN`. Keep failed results
in the report. State why a check was skipped or not run, and what that leaves
unverified. Do not turn an unavailable tool, credential, platform, or provider
into a pass.

Keep these evidence levels separate. A result at one level does not prove a
different level:

| Evidence | What it establishes | What it does not establish |
| --- | --- | --- |
| Mock or fixture test | The tested logic handles the supplied synthetic inputs. | That the actual command, package, runtime, platform, or provider works. |
| Actual CLI entry point | The checked-out command starts and performs the tested operation. | VSIX installation, native RCC/JAT execution, another platform, or live-provider behavior. |
| VSIX package check | The built extension package contains the checked files and metadata. | Successful installation or behavior on a target platform. |
| Native RCC/JAT run | The tested operation works with the named RCC and JAT artifacts. | Other versions, platforms, or providers. |
| Platform qualification | The named checks passed on the named operating system and architecture. | Untested platforms or live-provider behavior. |
| Live-provider check | The named operation worked against the named real provider under the recorded conditions. | Other providers, credentials, environments, or operations. |

For release and promotion work, perform a cheap preflight against the actual
repository checkout before expensive platform qualification:

1. Run the actual entry point, not only a helper or fixture path.
2. Check that the real checkout has the files and layout the operation expects.
3. Exercise the plan or dry-run path when available, then inspect its exact
   planned inputs and outputs.
4. Run solve/build, qualification, and promotion as separate steps. A metadata
   update, lock-file edit, or plan-only result does not establish that those
   steps succeeded.

The 2026-10-13 RCC release work showed why these checks need separate evidence:
fixture tests passed while the actual CLI import path failed; the template
checkout did not contain the root `release-lock.json` assumed by first
promotion; and Windows checkout representation affected source-integrity
verification. A metadata or lock update did not establish solve, build,
qualification, or promotion. These are preflight lessons, not reasons to relax
release protections.

Bind each release artifact and result to the exact source SHA, product or
runtime version, file digest, platform and architecture, workflow and run, and
dependency pins that produced it. Record these values in a sanitized receipt or
link to retained workflow evidence. Confirm that the checkout used for
verification is the checkout named by the evidence. Source provenance and
functional correctness are separate claims; record evidence for both.

For cross-platform source-integrity checks, account for platform checkout
representations such as line endings and file modes. State which representation
the check verifies. Do not claim portability from one platform's run.

Use synthetic names, paths, identities, catalogs, snapshots, and credentials in
examples and receipts. Never add secrets or private/customer data. Keep existing
security boundaries, required review gates, and publication protections intact.

## Repository format and Josh Room policy

Josh Room's rules in this guide are project policy. They are stricter than the
format requirements of the open agent-instruction and Agent Skills formats.

`AGENTS.md` is an open Markdown convention with no required heading schema. See
the [AGENTS.md format](https://agents.md/). Josh Room uses the root
[`AGENTS.md`](AGENTS.md) for concise repository-wide agent boundaries and links
to this guide for the PR evidence procedure. More-specific nested instructions
may apply to files in their directory; see [ChatGPT's layered AGENTS.md
guidance](https://learn.chatgpt.com/docs/agent-configuration/agents-md).

An Agent Skill is a directory with a `SKILL.md` file. The open specification
requires YAML frontmatter with `name` and `description`; the `name` must match
the skill directory. See the [Agent Skills specification](https://agentskills.io/specification).
Codex discovers repository skills under `.agents/skills/`; see [Build
skills](https://learn.chatgpt.com/docs/build-skills). These formats do not
certify a repository's policy or prove that a skill's advice produces correct
behavior. Josh Room's project requirements are the rules above, not a claim of
universal “2026 agentic standards” certification.

## Focused skill

Use [Josh Room PR evidence](.agents/skills/josh-room-pr-evidence/SKILL.md) when
preparing, reviewing, or revising a pull request. Keep the detailed procedure in
this guide; the skill provides task-specific prompts and small scenario
evaluations that check whether it applies the procedure correctly.
