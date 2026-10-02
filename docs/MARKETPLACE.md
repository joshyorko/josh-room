# Install Josh Room from the Visual Studio Marketplace

After the first publication is complete, Josh Room will be available as [`joshyorko.josh-room`](https://marketplace.visualstudio.com/items?itemName=joshyorko.josh-room). Until then, Marketplace search and ID installation are unavailable. After publication, in VS Code or VS Code Insiders, open Extensions and search for **Josh Room**, or install by ID:

```sh
code --install-extension joshyorko.josh-room
code-insiders --install-extension joshyorko.josh-room
```

After publication, devcontainers can install the extension through their normal VS Code extension provisioning:

```json
{
  "customizations": {
    "vscode": {
      "extensions": ["joshyorko.josh-room"]
    }
  }
}
```

## Exact-candidate publication

The Marketplace release is operator-owned and manual. The initial candidate is the already-promoted v0.1.26 VSIX from the GitHub release; do not rebuild it for Marketplace publication. Download and verify the package from the repository root:

```sh
ghx release download v0.1.26-standalone-vsix \
  --repo joshyorko/josh-room \
  --pattern josh-room-0.1.26.vsix \
  --dir /tmp/josh-room-marketplace
python3 scripts/verify_release_candidate.py \
  --candidate /tmp/josh-room-marketplace/josh-room-0.1.26.vsix \
  --tag v0.1.26-standalone-vsix
python3 scripts/verify_marketplace_candidate.py /tmp/josh-room-marketplace/josh-room-0.1.26.vsix
```

After verification, publish that exact file from `vscode-extension/` using `vsce`:

```sh
npx --yes @vscode/vsce publish --packagePath /tmp/josh-room-marketplace/josh-room-0.1.26.vsix
```

`vsce publish --packagePath` accepts an existing VSIX, preserving the accepted bytes. The command requires an authenticated Marketplace publisher session with permission to publish as `joshyorko`; never put a Personal Access Token in shell history, arguments, environment dumps, or repository files. Microsoft currently recommends Microsoft Entra ID for secure automated publishing; any automation decision belongs in a separate follow-up.

The package publisher ID must exist and be controlled by the operator before publication. Public gallery lookups currently show no listing for this extension ID, and public endpoints cannot establish publisher ownership. Verify publisher access in the Marketplace publisher management portal during the operator-authenticated publication session. If `joshyorko` cannot be used, stop: changing publisher identity changes the extension ID and requires a separately reviewed manifest and package update.

## Direct VSIX fallback

Direct VSIX installation remains supported for environments without Marketplace access. Download the standalone VSIX from the [GitHub release](https://github.com/joshyorko/josh-room/releases/tag/v0.1.26-standalone-vsix), then use **Install from VSIX…** in the Extensions view or `code --install-extension <path-to-vsix>`.

## References

- [Microsoft: Publishing Extensions](https://code.visualstudio.com/api/working-with-extensions/publishing-extension)
- [Josh Room standalone VSIX releases](https://github.com/joshyorko/josh-room/releases)
