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
