# Standalone Josh Room VSIX Runtime Design

## Goal

Install one Josh Room VSIX into a supported Linux amd64 VS Code/DevPod host
that has no Room of Requirement, Homebrew, RCC, Hauler, Action Server, Josh
Room CLI, or system JAT tooling. The extension owns bootstrap and invokes only
its version-matched runtime.

## Boundaries

- Josh Room owns native VS Code UX, storage/provider state, Room lifecycle,
  controller source, controller recipe, and managed runtime metadata.
- JAT remains a separately versioned capture/restore/serve substrate. The VSIX
  carries a clearly labeled JAT source contract and consumes a separately
  pinned JAT RCC Environment Artifact; it does not copy JAT dependencies into
  the Josh Room controller environment.
- RCC v18.19.2 owns both controller environment construction and JAT artifact
  acquisition/materialization/execution.
- Hauler is visible only inside the acquired JAT Holotree.

## Bootstrap and storage

The VSIX runtime manifest pins RCC `v18.19.2`, asset `rcc-linux64`, URL
`https://github.com/joshyorko/rcc/releases/download/v18.19.2/rcc-linux64`, and
SHA256 `3a90a331325feb5b75b3ebc7492303a964438ce017347f451aeee3ed7d578b3d`.
The extension downloads into a private temporary file, verifies the complete
binary, chmods it, atomically promotes it below `globalStorageUri`, and
validates `rcc version`. It never uses an ambient `rcc` from `PATH`.

All managed state lives below the extension's VS Code global storage: the RCC
binary, private `ROBOCORP_HOME`, controller source/materialization metadata,
JAT source contract, JAT archive cache, operation state, and logs. Every RCC
invocation receives `RCC_HOLOTREE_MODE=private` and the same private home.

RCC v18.19.2 does not implement GHCR/OCI providers. The JAT archive therefore
comes from an exact public GitHub Release asset whose archive SHA256 and RCC
artifact digest are pinned in the manifest. The extension verifies the
archive, invokes `rcc env acquire --archive --permissive-local --json`, checks
the returned canonical digest, and runs JAT with
`rcc env exec --artifact`. No `oras` or host tar is used by the extension.

## Controller and credentials

The VSIX contains the matching `josh_room` Python source, `controller/robot.yaml`,
and a frozen Linux controller environment configuration. The extension runs
the controller through the managed RCC task boundary, passing workspace and
runtime paths explicitly. The controller's RCC environment owns `age` and
Python dependencies. The controller does not launch a global `josh-room`.

VS Code `SecretStorage` holds MinIO credential JSON under opaque profile keys.
For each controller operation the extension creates a mode-0600 temporary
runtime credential file, passes only its path and a profile selector to the
controller, and removes it after exit. The standalone CLI's Secret Service
adapter remains available; `secret-tool` is not an extension prerequisite.

## Verification

Node tests prove platform mapping, checksum/atomic promotion, no PATH fallback,
private environment propagation, controller source identity, credential
handoff, and terminal Serve routing. Python tests prove controller/JAT process
contracts and no Action Server requirement. `vsce package` inspection proves
the VSIX includes all Josh Room-owned source and metadata. The final clean
Ubuntu acceptance is human-owned and remains explicitly unrun until Josh
installs the resulting VSIX in fresh containers.
