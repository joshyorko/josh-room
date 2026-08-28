# Standalone Josh Room VSIX Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Josh Room independently installable from a VSIX on plain supported Linux amd64 hosts.

**Architecture:** A VSIX-owned runtime module acquires one pinned RCC binary and uses a private global-storage RCC home. Packaged controller source runs through a managed RCC task; the controller invokes the separately acquired JAT artifact with `env exec`, while SecretStorage supplies secure credential handoff.

**Tech Stack:** VS Code Extension API, Node.js, Python 3.13, RCC v18.19.2, VSIX/`@vscode/vsce`, pytest, Node test runner.

**Spec:** `docs/superpowers/specs/2026-08-27-standalone-vsix-runtime-design.md`

## Global Constraints

- Supported platform for this release is Linux x64/amd64 only; unsupported platforms fail with a clear message.
- RCC asset is `rcc-linux64` at v18.19.2 with SHA256 `3a90a331325feb5b75b3ebc7492303a964438ce017347f451aeee3ed7d578b3d`.
- Production code never invokes ambient `rcc`, `josh-room`, `hauler`, `oras`, `age`, `action-server`, or `brew` from `PATH`.
- `RCC_HOLOTREE_MODE=private` and a private `ROBOCORP_HOME` are always propagated.
- Controller and JAT version identities are pinned together; stale global Josh Room CLI cannot be selected.
- Human fresh-container acceptance is the final gate and is not claimed by automated tests.

### Task 1: Add managed runtime acquisition primitives

**Files:**
- Create: `vscode-extension/runtime.js`
- Create: `vscode-extension/runtime.test.js`
- Create: `vscode-extension/runtime/manifest.json`

**Interfaces:**
- `resolvePlatform({platform, arch})` returns `linux-x64` only for `linux/x64` and rejects other combinations.
- `ensureManagedRcc(context, manifest)` returns `{executable, storageRoot}` and verifies cached files before reuse.
- `runtimeEnvironment(context, manifest, workspace)` returns the private RCC/JAT/controller environment map.

- [ ] **Step 1: Write failing tests for platform mapping, checksum mismatch, atomic promotion, cache reuse, and private environment values.**
- [ ] **Step 2: Run `node --test vscode-extension/runtime.test.js` and observe missing module behavior.**
- [ ] **Step 3: Implement HTTPS download, streaming SHA256 verification, chmod, atomic promotion, version validation, and private path resolution.**
- [ ] **Step 4: Run the focused runtime tests and confirm green.**

### Task 2: Package and execute the matching Python controller

**Files:**
- Create: `vscode-extension/runtime/controller/robot.yaml`
- Create: `vscode-extension/runtime/controller/conda.yaml`
- Create: `vscode-extension/runtime/controller/environment_linux_amd64_freeze.yaml`
- Create: `vscode-extension/runtime/controller/josh_room/` source mirror
- Modify: `src/josh_room/jat.py`
- Modify: `src/josh_room/keyring.py`
- Modify: `src/josh_room/cli.py`
- Modify: `tests/test_jat.py`, `tests/test_auth.py`, and credential tests

**Interfaces:**
- The controller task is invoked by managed RCC with `PYTHONPATH` rooted in the VSIX controller source.
- `josh_room.jat` consumes `JOSH_ROOM_RCC_EXE`, `JOSH_ROOM_RCC_HOME`, `JOSH_ROOM_JAT_ROOT`, `JOSH_ROOM_JAT_ARTIFACT`, and a progress/receipt path; it never uses a bare `rcc` or global JAT root.
- Extension runtime credentials are a mode-0600 JSON file with either one credential object or a profile map; standalone keyring behavior is unchanged.

- [ ] **Step 1: Add failing tests for managed RCC argv/env, source SHA matching, no `git` requirement, and profile-scoped runtime credentials.**
- [ ] **Step 2: Run `UV_CACHE_DIR=/tmp/uv-cache uv run --group dev python -m pytest tests/test_jat.py tests/test_auth.py -q` and observe failures.**
- [ ] **Step 3: Implement the managed JAT `env exec` contract and extension runtime credential path.**
- [ ] **Step 4: Add the controller recipe and frozen configuration with `age` in the RCC-managed environment.**
- [ ] **Step 5: Run focused Python tests and `ruff check` on touched Python files.**

### Task 3: Replace all extension global-CLI launches

**Files:**
- Modify: `vscode-extension/extension.js`
- Modify: `vscode-extension/extension.test.js`
- Modify: `vscode-extension/package.json`

**Interfaces:**
- `runJoshRoom` ensures the managed runtime and spawns the controller task through the managed RCC binary.
- Registry terminal commands use an extension-owned launcher that invokes the same managed RCC/controller path; no terminal text contains `josh-room` as an executable lookup.
- MinIO create/update/reconnect stores credentials in `SecretStorage` while retaining stdin handoff to the backend.

- [ ] **Step 1: Add failing spawn-harness assertions that command launches use the managed RCC path, private home, controller root, and JAT metadata.**
- [ ] **Step 2: Run the extension focused tests and observe bare `josh-room` launches.**
- [ ] **Step 3: Implement managed controller execution, cancellation, progress log paths, Serve terminal launcher, and SecretStorage integration.**
- [ ] **Step 4: Run all Node extension tests and inspect the exact spawned argv/env.**

### Task 4: Package the actual VSIX and reconcile host assumptions

**Files:**
- Modify: `vscode-extension/package.json`
- Modify: `release-lock.json`
- Modify: `README.md`, `docs/architecture.md`, and repository-boundary tests
- Modify: `.devcontainer/bootstrap.sh`, `templates/room/.devcontainer/bootstrap.sh` only to remove Action Server/global Josh Room runtime requirements while retaining the optional golden host
- Produce: `dist/josh-room-0.1.2.vsix`

- [ ] **Step 1: Add a packaging test that inspects the VSIX for runtime manifest, controller source, recipe, JAT source contract, and no secrets.**
- [ ] **Step 2: Run the package test and observe missing VSIX contents/scripts.**
- [ ] **Step 3: Add standard `vsce` packaging scripts and update immutable runtime metadata from the JAT receipt.**
- [ ] **Step 4: Remove Action Server from live/golden bootstrap paths where no source call requires it; classify CLI-only Secret Service and standalone host IDE integration as optional.**
- [ ] **Step 5: Run `npx @vscode/vsce package --no-dependencies`, inspect archive contents, and run `git diff --check`.**

### Task 5: Prepare the human acceptance handoff

**Files:**
- Evidence only outside Git repositories

- [ ] **Step 1: Build/publish the new JAT artifact and public release asset.**
- [ ] **Step 2: Build and inspect the real VSIX.**
- [ ] **Step 3: Record automated source/build/runtime gates separately from the clean-container gate.**
- [ ] **Step 4: Hand Josh the exact VSIX path, RCC/JAT/Hauler identities, and a two-container acceptance command sheet; report clean acceptance as unrun.**
