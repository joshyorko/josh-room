# Room View (`josh-room view`)

Room View provides two read-only ways to observe and review any Room tool's
sanitized state without building a separate web application per tool:

1. **Live localhost viewer** (`josh-room view serve`): binds strictly to
   `127.0.0.1` (default port `0`), polls a sanitized `ViewStateProvider` on a
   background collector thread, serves read-only `GET`/`HEAD` endpoints, and
   updates the shared viewer every few seconds.
2. **Single-file offline view snapshot** (`josh-room view export`): writes one
   self-contained `.html` file containing an embedded `room-view-v1` SQLite
   database and pinned, gzipped, base64-encoded viewer libraries (`three`,
   `sql.js` + WASM, `pdf.js` + worker). Opening the file from `file://` makes
   **zero network requests** under `connect-src 'none'`.

All examples, fixtures, receipts, and screenshots in this repository use
**synthetic data only**.

## Standalone VS Code extension

In desktop VS Code with a **Linux workspace extension host** (local, devcontainer,
attached container or Remote SSH), open an entered Room folder, right-click
its Room row in the Josh Room sidebar and choose **Explore Room in 3D…**.
The Command Palette also offers **Josh: Explore Room in 3D…** with an explicit
choice among open, entered Rooms. The marker must match the folder, Dimension,
and Room. The controller verifies the Room/Dimension, encryption domain,
namespace and directory identity against the original selection at startup
and pins them for the session. A changed or removed binding invalidates the session's workspace
state and attachments; launch it again after entering the intended Room. Save
within the same Room does not invalidate the viewer. Enter/open a Room first if
it is not loaded in this window.

This uses the VSIX's private managed controller, not a global `josh-room`
command. It observes the current on-disk workspace without Save, hydration, or
a new Logical JAT. Unsaved editor buffers are not included. The browser opens
the actual allocated `127.0.0.1` URL with a per-session token; do not share that
URL. The existing capture-policy, bounded read, sanitizer and sensitivity gates
apply. Workspaces overlapping the managed RCC runtime directory are refused;
that directory is never exposed as Room content. There is no webview or hosted service.

Use **Josh: Stop Room 3D Viewer** to stop the owned session. Starting another
viewer asks to stop the existing one. Closing the VS Code window or changing
workspace folders also stops it. Closing the browser tab alone does not stop
the session: browser tab lifetime cannot be observed reliably by the extension.
Startup can be cancelled from its progress notification, including during
browser handoff. Stop/deactivation also invalidates pending selection prompts.
Only a small OS environment allowlist and the managed runtime's own non-secret
settings reach the viewer; ambient provider credentials are not inherited.

The desktop client and browser may run on Windows; the workspace extension and
controller must run on Linux. In devcontainer/attached-container/SSH windows,
Josh Room uses VS Code's `asExternalUri` to forward the tokenized server URL to
the desktop. `remote.localPortHost` must be `localhost`, and the returned URL
must be an HTTP desktop loopback address preserving the token. Token-protected
sessions accept a remapped loopback Host/port; non-loopback Host headers stay
forbidden. Public tunnel
URLs and all-interfaces forwarding are refused. No Docker port publication or
manual listener exposure is needed. Stop removes the owned Linux listener;
VS Code owns the forwarding entry, which may remain in the Ports view after
Stop but cannot serve Room data once the viewer has stopped.

Native Windows/macOS controllers, WSL, Codespaces, other remote authorities,
and web-hosted VS Code remain unsupported for this action. These limits do not
change the platform support of other Room commands.

**Browse Recovery Point…** remains historical Time Machine recovery.
**Inspect Logical JAT** inspects saved metadata; **Serve Room Content…** and
**Serve this Logical JAT…** remain Hauler files/registry projections. Historical
nested JAT contents are not offered as live 3D workspaces.

### Installed launcher verification

`scripts/installed_room_view_acceptance.js` is a VS Code extension-test driver.
Run it with `--extensionTestsPath` in a disposable development-host profile,
with the candidate VSIX installed in a task-private `--extensions-dir` and
`JOSH_ROOM_ACCEPTANCE_ROOT` pointing to that task directory. Open a trusted
synthetic Room named `synthetic-room` in `synthetic-dimension` with a path-bound
marker. The driver executes the registered Explore and Stop commands and uses
the actual pinned managed controller. It intercepts only the browser handoff,
checks authenticated HTTP 200, actual workspace state, unauthenticated/method
denials, and listener shutdown, and writes metadata-only
`installed.jsonl` evidence. This does **not** qualify visible browser rendering,
3D navigation, or OS default-browser integration.

For a remote desktop-to-container run, install the candidate and acceptance
extension in the Linux workspace host. Register a desktop **UI-kind** test
command that calls `scripts/installed_room_view_probe.js` (`probe` / `isStopped`)
on the returned URL, and set `JOSH_ROOM_DESKTOP_PROBE_COMMAND` to that command
in the workspace test driver. The remote driver calls the real `asExternalUri`;
HTTP assertions execute on the desktop, not inside the container. The Linux
side separately verifies that Stop removed its owned listener. This separates
actual forwarding evidence from a same-host or mocked-URI test. Windows desktop
qualification still requires an actual Windows client run.

## CLI contract

```text
josh-room view serve  --provider <name|module:attr> [--port 0] [--interval 5] [--workspace <path>] [--manifest <file.json>] [--require-token] [--expected-room-id <id> --expected-dimension-id <id>] [--json]
josh-room view export --provider <name|module:attr> --output <file.html> [--no-attachments] [--max-size 50MB] [--sensitivity-ack] [--workspace <path>] [--manifest <file.json>] [--room-id <id>] [--json]
josh-room view export --from-state <state.json> [--attachments-dir <dir>] --output <file.html> [--no-attachments] [--max-size 50MB] [--sensitivity-ack] [--json]
josh-room view inspect <file.html> [--json]
```

Built-in native providers:

- `room --workspace <dir>` — bounded read-only workspace observation using Room
  identity, capture policy, descriptor-relative no-follow reads, mount checks,
  and typed sanitized previews. Requires safe descriptor-relative file APIs
  (verified on Linux); unsupported platforms fail closed. Defaults: 250 nodes,
  depth 6, 64 children per directory, 5 seconds, 16 KiB per preview and 512 KiB
  total preview content. Caps produce explicit omissions, not complete coverage.
  There is no continuation cursor yet. Incomplete sibling scans are withheld
  rather than admitting names before all aliases are checked.
- `capsule-manifest --manifest <file.json>` — validates a saved Logical JAT
  descriptor and displays recorded workspace/component anchors. This validates
  manifest structure, not the existence or digest of underlying archive bytes;
  it neither extracts nested files nor starts Hauler.

Both native providers default to `internal`: offline export needs
`--sensitivity-ack`. Secret-named files are never opened; detected sensitive
text/JSON becomes a `redacted` preview. Small PDF/image attachments remain
binary evidence, not content-sanitized text. Attachment reads verify bytes and
recheck current capture policy. Live workspace observation is not an atomic
snapshot or Save authority.

Built-in synthetic providers for testing and demos:

- `synthetic-unix` — original synthetic `/usr`, `/etc`, `/var`, `/home`, `/src`
  hierarchy with source, diff, JSON, execution log and linked evidence examples.

- `synthetic` — 3 groups × 6 stages = 300 synthetic items with deduplicated
  synthetic PDFs, before/after PNGs, and inert HTML reports.
- `synthetic-small` — 10 synthetic items across 2 groups × 3 stages.
- `synthetic-large` — 540 synthetic items across 4 groups × 6 stages.
- `synthetic-sensitive` — 300 synthetic items marked `confidential` to exercise
  the fail-closed export sensitivity gate.

Custom providers may register a Python entry point in the
`josh_room.view_providers` group or be passed directly as `module:attr`.

## `ViewStateProvider` contract

`josh_room.view.contract.ViewStateProvider` is a closed, transport-neutral
protocol:

```python
class ViewStateProvider(Protocol):
    def describe(self) -> ViewDescriptor: ...
    def state(self) -> ViewState: ...
    def attachment(self, item_id: str, name: str) -> Attachment | None: ...
```

Key validation rules:

- Identifiers (`group.id`, `stage.id`, `item.id`, `kind`, `tool`) must match
  the safe-label grammar `^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$`.
- Attachment names must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$` with no
  `..` segments or path separators. The view layer never resolves paths from
  identifiers except through `provider.attachment(item_id, name)`.
- Attachment media types allowlisted for embedding in v1 are
  `application/pdf`, `image/png`, `image/jpeg`, `image/webp`, and `text/html`.
  Other declared media types are listed in metadata with `bytes = NULL` and are
  never embedded.
- Referential integrity is enforced across groups, stages, legend kinds, and
  lineage steps.
- Descriptor SQL presets must be strictly read-only (`SELECT`/`WITH`/`EXPLAIN`).

## Mandatory sanitization & sensitivity model

- **Server-side & pre-export sanitization** (`josh_room.view.sanitizer`):
  rejects oversized free text (labels > 96 chars, fields/summary > 160 chars),
  control characters, long digit runs (> 8 consecutive digits), secret-like keys
  (`token`, `secret`, `password`, `credential`, `api_key`, `identity`, etc.),
  key/token values (`AGE-SECRET-KEY-`, `age1...`, AWS keys, `Bearer ...`),
  host paths (`/home/...`, `/Users/...`, `C:\...`), and private endpoints.
- Providers may define an optional `sanitize(state) -> ViewState` hook to
  tighten sanitization further, but the base validator/sanitizer runs both
  before and after the hook so it can never be bypassed.
- **Fail-closed sensitivity gate**: `resolve_sensitivity()` reads both the
  provider's `descriptor.sensitivity` and any `# josh-room sensitivity: <level>`
  directive in the workspace `.josh-roomignore`, selecting the **most
  restrictive** marking. Any marking other than `public` or `synthetic` causes
  `view export` (and live `/export.html`) to **refuse by default** until
  explicitly acknowledged (`--sensitivity-ack` or confirmed live dialog).
- Exported snapshots never auto-publish, upload, or create sharing links, and
  are written atomically through a private `0600` temporary file.

## Live server security (`josh-room view serve`)

- Uses Python stdlib `ThreadingHTTPServer` bound strictly to `127.0.0.1`.
- **DNS-rebinding defense**: rejects any request whose `Host` header is not
  `127.0.0.1:<port>` or `localhost:<port>` with `403 Forbidden`.
- **Read-only**: only `GET` and `HEAD` are permitted; `POST`, `PUT`, `DELETE`,
  `PATCH`, and `OPTIONS` return `405 Method Not Allowed`.
- Sets `X-Content-Type-Options: nosniff`, `Cross-Origin-Resource-Policy: same-origin`,
  `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, `Cache-Control: no-store`,
  and a strict Content-Security-Policy (`connect-src 'self'`). HTML attachments
  add `Content-Security-Policy: sandbox`.
- Optional `--require-token` requires a per-session random URL/header token.
- Background collector exceptions never crash the server and never surface raw
  upstream exception strings; instead `/state.json` records a sanitized `stale`
  object (`reason: "collector-error"`).

## Embedded `room-view-v1` SQLite schema

```sql
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE groups(id TEXT PRIMARY KEY, name TEXT, ord INT);
CREATE TABLE stages(id TEXT PRIMARY KEY, name TEXT, ord INT, terminal INT, failure INT);
CREATE TABLE items(id TEXT PRIMARY KEY, group_id TEXT, stage_id TEXT, kind TEXT, label TEXT, metric REAL, updated_at TEXT, fields_json TEXT);
CREATE TABLE steps(item_id TEXT, name TEXT, at TEXT);
CREATE TABLE attachments(item_id TEXT, name TEXT, media_type TEXT, size INT, sha256 TEXT, bytes BLOB, PRIMARY KEY(item_id, name));
CREATE TABLE attachment_blobs(sha256 TEXT PRIMARY KEY, bytes BLOB);
CREATE TABLE summary(key TEXT PRIMARY KEY, value TEXT);
```

Payloads are deduplicated by SHA-256 into `attachment_blobs`, `VACUUM`ed,
gzipped deterministically (`mtime=0`), base64-encoded, and injected as
`<script id="room-view-db" type="application/octet-stream" data-schema="room-view-v1" data-encoding="gzip">`
immediately before the **last** `</body>`.

## Offline template, CSP, and pinned libraries

Pinned versions and SHA-256 digests live in `src/josh_room/view/vendor-lock.json`
and are embedded into `<meta name="josh-room-view">` by
`scripts/build_view_template.py`:

- `three@0.170.0` (MIT)
- `sql.js@1.12.0` + `sql-wasm.wasm` (MIT)
- `pdfjs-dist@4.10.38` + `pdf.worker.min.mjs` (Apache-2.0)

Offline exports enforce:

```text
default-src 'none'; connect-src 'none'; script-src 'unsafe-inline' blob: 'wasm-unsafe-eval';
worker-src blob:; style-src 'unsafe-inline'; img-src 'self' blob: data:; font-src data:;
object-src 'none'; base-uri 'none'; form-action 'none';
```

At runtime the viewer decodes libraries and the embedded SQLite database purely
in memory (`Uint8Array.fromBase64` / chunked `atob` + `DecompressionStream('gzip')`),
registers a `blob:` import map for `three`, passes `wasmBinary` directly to
`initSqlJs`, and never calls `fetch('data:...')` or contacts any CDN.

HTML attachments are never executed: they are parsed inertly with `DOMParser`
into a structured comparison view with scripts/iframes/objects stripped.

## 3D FSN landscape, accessibility, and size accounting

- **Landscapes**: full-bleed **Night** (default: zenith `#04060c`, horizon
  `#1a2840`, matching `FogExp2`, anti-aliased infinite `fwidth` world grid) and
  FSN-inspired **Grass** (`#0087d5` → `#91fff0` sky, `#0f4f1a` → `#418a59`
  ground). Uses `NeutralToneMapping` and FSN-style restraint (~0.6× unselected,
  1.0× selected).
- **Hierarchy & scale**: foreground root pedestal → branching group pedestals →
  stage pedestals connected by parent-child ground wires → upright uniform-footprint
  file blocks whose heights represent **5 quantile bins** of the metric. World
  geometry stays constant in size as item counts grow: pedestals with >16 items
  show the 11 highest-interest items (failures first, then metric) plus an
  expandable `+N more` stacked-outcome block; groups with >50 items collapse
  into stage summary blocks until focused. Distance LOD simplifies far blocks
  and pedestals with ~15% hysteresis.
- **Spotlight v2 & A/B baseline**: persistent `SpotLight` (never toggled at
  click time; shaders precompiled at boot) with a `SpotLight.map` radial pool,
  `FrontSide` apex-faded dithered volumetric beam cone, world dimming (lights,
  wires, grid, fog) over 450 ms, and **no literal lamp/fixture**. Baseline
  prototype parameters remain selectable (`Spot: base` / `?spotlight=baseline`)
  for side-by-side comparison.
- **Accessibility & fallback**: `<canvas aria-hidden="true">` paired with a
  bidirectional APG `role="tree"` twin, polite `aria-live="polite"` landing
  announcer, WCAG 1.4.1 non-colour outcome shape cues (solid Pass, `×`-crown
  Fail, hollow-cage Skip), keyboard navigation (`↑/↓/→/←`, `Home`/`End`,
  `Enter`, `Esc`, `F`, `/`, `[`/`]`, `G`, `M`, `?`), reduced-motion cuts
  preserving opacity spotlight fades, automatic 2D Grid fallback when WebGL is
  unavailable, and print layout combining a static top-down map capture with
  the 2D triage table.
- **Size accounting**: `view export`, `view inspect`, and the live export modal
  report template bytes, raw/gzip/base64 DB bytes, referenced vs. unique
  deduplicated attachment bytes, and final HTML size. `--max-size` and
  `--no-attachments` enforce export size budgets.

## Click-through interaction contract (`NAVIGATE` / `INSPECT` / `ACT`)

`NAVIGATE` and `INSPECT` are implemented in this PR. `ACT` remains out of scope.

| Target | Read-only behavior |
| --- | --- |
| Directory pedestal | Enter/focus its branch; parent-child wires, breadcrumbs and Back preserve location. |
| Summary tower | Expand aggregated children at unchanged world-space dimensions. |
| File, grid tile, tree or search result | Fly/select, spotlight, and open the dismissible inspector. |
| Inspector content | Source/config/log text with line numbers, diff highlighting, collapsible JSON, structured execution details, PDF/image/inert HTML evidence and timeline. |
| Relationship link | Jump to its producer, output, consumer or other declared object, without network access. |

Providers may supply `ViewState.nodes`: `{id, parent_id, node_kind, label,
updated_at, kind?, metric?, fields?, previews?, attachments?, links?}`. Trees are
validated for unique IDs, valid parents and cycles. Legacy group/stage/item
providers map automatically to root → group → stage → item. Search, ARIA tree,
grid and SQL retain nodes hidden by 3D aggregation or LOD.

`PreviewContent` stores a bounded typed body, slot, title, language, summary and
status (`available`, `unavailable`, `redacted`, `omitted`). `ObjectLink` stores a
validated relation and target ID. Preview and relationship data passes the same
mandatory sanitizer as descriptor/state data. Live content uses only validated
`GET /content/<id>/<slot>`; there is no arbitrary path route. Offline snapshots
embed the same content and links with zero network.

Additive SQLite tables in `room-view-v1` are `nodes` (including computed `path`),
`previews` (keyed by `node_id, slot`), and `links` (source/relation/target).
Missing, redacted or capped content renders an explicit notice. Refreshing live
state updates an open inspector while preserving its tab, or closes it when
its item disappears.

The viewer never starts native artifact serving. Extension Room context menus
invoke the existing logical-JAT `snapshot serve --mode files|registry|both|auto`
path; `JAT: Serve Haul…` independently serves a selected portable archive.
See [README](../README.md#standalone-vsix) and
[ADR 0012](adr/0012-room-view-native-serving-and-hauler-spike.md) for the native
serving distinction and isolated Hauler experiment.

No browser action endpoints, filesystem writes, retry/approve controls, hosted
UI or VS Code webview are implemented. Human recognition, lookup studies and
physical GPU qualification remain pending; synthetic browser checks cannot
certify those gates.
