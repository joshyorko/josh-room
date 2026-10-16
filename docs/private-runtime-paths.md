# Private Room Store runtime paths

Room Store password, exclude, and restic-cache paths are caller-owned temporary
runtime data. Their directories and files must remain outside the captured
workspace and must pass `josh_room.private_paths` protection before use.

On Linux, directory helpers set mode `0700`; file helpers set mode `0600`.
Verification checks the current owner, object type, final-component no-follow
status, and exact private mode. On Windows, helpers set the current user as
owner and apply a protected DACL with full access only for that user and
`SYSTEM`. Directories grant object and container inheritance; files grant no
inheritance. Verification rejects inherited, extra, missing, or broader ACEs,
wrong owners, and reparse points. If native security APIs are unavailable or
fail, the operation stops with a path-free error.

These helpers protect transient files only. They do not store credentials.
Persistent provider credentials remain in the host operating system's
credential service or keyring. An ephemeral runtime credential supplied by a
caller is still only accepted in the already-protected runtime directory.

## Runtime artifact cache and system-tool discovery

Controller and JAT archive cache filenames include the pinned archive SHA256.
A new pin gets a distinct cache entry; an older basename-only entry is preserved
and the new archive is downloaded and verified once. Same-pin size/checksum
failures and symlink/nonregular cache entries still fail closed. No user cache
is silently pruned during an upgrade.

Linux RCC subprocess environments preserve the existing PATH order and append
`/usr/sbin` and `/sbin` for genuine system inventory tools such as `ldconfig`.
This affects initial acquisition as well as later managed execution. It does
not bypass artifact compatibility checks. Explicit local-build fallback forwards
stdin through RCC's interactive stream mode so credential JSON remains off argv.

Windows controller launches preserve an inherited `Path` or other case variant
and pass one canonical `PATH` key. Explicit runtime overrides take precedence;
existing directory order is preserved. This keeps system tools such as `cmd.exe`
available when VS Code supplies mixed-case environment keys.
