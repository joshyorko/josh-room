---
name: josh-room-pr-evidence
description: Prepare or review a Josh Room pull request by stating documentation impact, reporting scoped verification evidence, and identifying remaining limits. Use when drafting, reviewing, or revising a pull request in this repository.
---

# Josh Room pull request evidence

Before preparing or reviewing a pull request, read the repository's
[`CONTRIBUTING.md`](../../../CONTRIBUTING.md). That guide is the canonical
source for documentation policy, evidence levels, release provenance, and
privacy rules.

For the pull request:

1. Identify the behavior, commands, dependencies, architecture, supported
   platforms, operations, or limits affected by the change.
2. Update the canonical docs for each affected contract. If none are affected,
   give a concrete no-documentation-impact reason.
3. List each verification command or workflow with its scope and observed
   result. Mark unavailable or unrun checks `SKIPPED` or `NOT RUN`, and retain
   failures as `FAIL`.
4. Keep mocked tests, actual CLI runs, VSIX checks, native RCC/JAT runs,
   platform qualification, and live-provider evidence distinct.
5. For release evidence, bind results to the exact source SHA, version, file
   digest, platform and architecture, workflow and run, and dependency pins.
   Verify the actual repository shape and command entry point before expensive
   qualification. Do not describe metadata-only or plan-only work as a full
   solve, build, qualification, or promotion.
6. State what remains unverified. Use synthetic examples and preserve all
   security and publication boundaries.

## Scenario evaluations

Use these small cases to check whether the skill applies the guide. They test
whether the advice is followed; valid frontmatter or format checks alone do not
prove behavior. See [Evaluating skill output quality](https://agentskills.io/skill-creation/evaluating-skills)
for the distinction between format validation and behavioral evaluation.

| Scenario | Expected result |
| --- | --- |
| RCC fixture tests pass while the actual CLI import fails; solve/build and platform qualification did not run. | Preserve the CLI failure as `FAIL`; report solve/build and qualification separately as `NOT RUN` or `SKIPPED`. Do not claim a release is qualified or promoted. |
| A passing artifact receipt names source SHA A, but the tested checkout is source SHA B. | Flag the provenance mismatch. Do not attribute the receipt to B or claim B passed; rerun from B or leave the claim unverified. |
| A change renames an internal local variable and leaves behavior, commands, dependencies, platforms, operations, and limits unchanged. | Do not make a meaningless doc edit. State the specific no-documentation-impact rationale and report the checks that were actually run. |
