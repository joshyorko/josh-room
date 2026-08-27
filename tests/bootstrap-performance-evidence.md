# Historical Josh Room bootstrap performance evidence

This document describes the superseded pre-VSIX host bootstrap and is retained
only as historical evidence. It is not the current runtime contract and must
not be used as standalone acceptance evidence. The current path is the
packaged VSIX acquiring managed RCC and the pinned JAT artifact.

Date: 2026-08-27

The measurements below use the pinned values already present in the bootstrap
and release lock. Temporary RCC homes were created empty before each proposed
path run, with `ROBOCORP_HOME=<temporary directory>` and
`RCC_HOLOTREE_MODE=private`. The artifact pull used host-provided registry
authentication; no credential or host configuration is stored here.

## Before: current bootstrap sequence

The current sequence was read from the pre-change bootstrap copies:

```text
brew tap joshyorko/tools
trust_tap joshyorko/tools
brew install age uv libsecret jq oras
brew install --cask joshyorko/tools/rcc joshyorko/tools/action-server
hash -r
test "$(rcc version | head -n 1)" = "$EXPECTED_RCC_VERSION"
sudo "$(command -v rcc)" ht shared --enable --once
rcc ht init
uv tool install --force "git+https://github.com/joshyorko/josh-room.git@${JOSH_ROOM_GIT_SHA}"
JAT checkout at $JAT_GIT_SHA
CONDA_PREFIX=... bash "$jat_root/scripts/install_dependencies.sh" 1
bash bootstrap-jat-environment.sh "$jat_root/robot.yaml"
bootstrap completion checks
```

Warm host measurements from the current sequence were:

| Phase | Result | Time |
| --- | --- | ---: |
| RCC install (`brew install --cask ...`) | pass, already installed | 1.021s |
| `rcc version` | pass, v18.19.2 | 0.706s |
| `rcc ht shared --enable --once` in empty private home | pass, non-sudo characterization | 0.265s |
| `rcc ht init` in empty private home | pass, non-sudo characterization | 0.235s |
| Josh Room tool install | pass, pinned Josh Room SHA | 3.144s |
| extension install | pass | 0.049s |
| JAT checkout | pass, exact pinned SHA | 0.444s |
| external dependency install | pass | 2.633s |
| artifact helper with temporary HOME and no registry auth | fallback build; artifact pull was unauthorized | 44.560s |

The exact current `sudo "$(command -v rcc)" ...` gate and its full postCreate
time were skipped: `sudo -n true` returned 1 because this workstation
requires a password. No password prompt was attempted.

## After: proposed sequence and acceptance

The edited root bootstrap was run as the real script with a fresh temporary
`ROBOCORP_HOME`, private Holotree mode, a temporary HOME/JAT checkout, and
host-provided registry authentication:

```text
brew tap joshyorko/tools
trust_tap joshyorko/tools
brew install age uv libsecret jq oras
brew install --cask joshyorko/tools/rcc joshyorko/tools/action-server
hash -r
test "$(rcc version | head -n 1)" = "$EXPECTED_RCC_VERSION"
uv tool install --force "git+https://github.com/joshyorko/josh-room.git@${JOSH_ROOM_GIT_SHA}"
extension copy
JAT checkout at $JAT_GIT_SHA
CONDA_PREFIX=... bash "$jat_root/scripts/install_dependencies.sh" 1
bash bootstrap-jat-environment.sh "$jat_root/robot.yaml"
bootstrap completion checks
```

Measured phase timings from the same clean proposed-path family were:

| Phase | Result | Time |
| --- | --- | ---: |
| RCC install (`brew install --cask ...`) | pass, already installed | 0.953s |
| `rcc version` | pass, v18.19.2 | 0.369s |
| Josh Room tool install | pass, pinned Josh Room SHA | 3.144s |
| extension install | pass | 0.049s |
| JAT checkout | pass, exact pinned SHA | 0.444s |
| external dependency install | pass | 1.826s |
| ORAS artifact download + RCC acquire + no-build vars | pass | 38.559s |
| real edited root postCreate bootstrap | pass | 44.863s |

Acceptance evidence from the successful run:

```text
JAT HEAD = 0d08869f1e0d267bed72c2a76ff32b376b8e10a1
artifact manifest = sha256:173cd4fe996af650257651152dac76abd96e9d90ea36b68d0cf7b17b09e02d50
artifact digest = sha256:36fa516220d023201cbc989624cf3e9c914ab36f5d206daee95303a876ae25ca
archive SHA-256 = 1e9837d5a1cf154a87aece9cbb607a98a468a1445715072559b15b6fde10b94b
archive size = 230662657
rcc env acquire = rc 0, verification.valid true
rcc --no-build ht vars = rc 0
fallback build message = absent
JAT Doctor task = rc 0
```

The direct fresh-home acquire also returned `artifactDigest` equal to
`sha256:36fa516220d023201cbc989624cf3e9c914ab36f5d206daee95303a876ae25ca`
with `verification.valid: true`. Its subsequent no-build RCC output showed
`no-build` active and no environment build phase.

## Necessity result

The no-early proposed path succeeded in a fresh private RCC home, including
artifact acquire, no-build activation, and the JAT Doctor task. A matched
fresh-home control that included non-sudo `rcc ht shared --enable --once` and
`rcc ht init` also succeeded. Therefore neither early command is required for
the pinned JAT Environment Artifact path. The exact sudo form was not claimed
as run because sudo is password-gated on the measurement host.

The former fallback in `bootstrap-jat-environment.sh` was part of the
superseded host bootstrap and is no longer present. The current extension path
fails closed on a missing or invalid pinned artifact; it does not fall back to
a host environment build.
