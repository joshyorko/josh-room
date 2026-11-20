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

## CLI contract

```text
josh-room view serve  --provider <name|module:attr> [--port 0] [--interval 5] [--workspace <path>] [--require-token] [--json]
josh-room view export --provider <name|module:attr> --output <file.html> [--no-attachments] [--max-size 50MB] [--sensitivity-ack] [--workspace <path>] [--room-id <id>] [--json]
josh-room view export --from-state <state.json> [--attachments-dir <dir>] --output <file.html> [--no-attachments] [--max-size 50MB] [--sensitivity-ack] [--json]
josh-room view inspect <file.html> [--json]
```

Built-in synthetic providers for testing and demos:

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

Room View is a **localhost-only, sanitized, read-only state viewer**—not a
general-purpose UNIX file server. To keep the SGI FSN 3D landscape genuinely
useful in daily Room workflows without weakening security or offline
portability, click-through behavior is structured around three distinct
capabilities:

### Current v1 behavior (implemented in #202)

| Click target | Current v1 action | What it displays |
| --- | --- | --- |
| Root `/` pedestal | Flies back to low-angle overview; clears spotlight; hides inspector; syncs breadcrumbs and APG `role="tree"`. | Full tree overview (with `>50`-item groups collapsed to stage summaries). |
| Group pedestal | Expands collapsed group (`>50` items) in place; swoops camera ~30° above pedestal; brightens root → group wire. | Group summary counts and fanned stage pedestals. |
| Stage pedestal | Swoops camera ~30° above stage pedestal; brightens root → group → stage wire. | Individual file towers (≤12–16) or 11 highest-interest items + `+N more` SpaceTree block. |
| `+N more` / stage-summary tower | Expands aggregated siblings or collapsed group in place and flies to the expanded branch. | Previously aggregated items at constant world-unit dimensions. |
| Ground wire | Selects child pedestal; second click ascends to parent (FSN convention). | Active parent-child branch highlight. |
| File / item tower (or Grid/Search hit) | Swoops camera ~40° above block; activates Spotlight v2 (no lamp); opens dismissible Inspector drawer; syncs APG tree & polite live announcement. Shift-click selects without camera flight; clicking the beam zooms closer. | **Details** (ID, group/stage, outcome cue, metric + quantile bin, sanitized scalar `fields`), **Evidence** (lazy `pdf.js` PDFs, side-by-side PNG/JPEG/WebP + ≤1.25× lightbox, inert `DOMParser` HTML reports, or explicit unavailability notice), and **Timeline** (ordered `steps`). |

### Absent capabilities & scoped follow-up architecture

The following capabilities are **intentionally outside #202 v1** and reserved
for a backwards-compatible follow-up issue (keeping existing
`ViewStateProvider` implementations working unchanged):

1. **`NAVIGATE` — arbitrary-depth provider-defined hierarchy (follow-up)**:
   - v1 fixes the hierarchy to four levels (`root → group → stage → item`).
   - Additive follow-up: optional `nodes` table/list
     (`{id, parent_id, node_kind, label, kind?, metric?, summary?}`) enabling
     provider-defined trees of arbitrary depth (e.g.
     `root → service → package → suite → run → artifact`) while mapping v1
     `groups`/`stages`/`items` onto the same model automatically. Aggregation
     (`>16` children, `>50` subtree) and distance LOD remain bounded while
     keeping every node reachable via APG tree, search, 2D Grid, and SQL.
2. **`INSPECT` — typed read-only content previews & relationship links (follow-up)**:
   - v1 limits leaf inspection to scalar `fields` (≤160 chars), step timestamps,
     and the five allowlisted attachment media types (`application/pdf`,
     `image/png`, `image/jpeg`, `image/webp`, inert `text/html`).
   - Additive follow-up schema separates five distinct read-only layers:
     - **Summary**: compact inline metadata (existing `ItemState` fields).
     - **Preview**: bounded, server-sanitized typed preview payloads (e.g.
       `text/plain`, `text/x-diff` with line numbers/diff highlighting,
       `application/json` collapsible tree, structured execution log/exit-code
       records) stored in an additive `item_previews(item_id, slot, media_type, status, reason, body_text)`
       table for identical live + offline HTML rendering.
     - **Content**: optional allowlisted read-only `content(item_id, slot)`
       provider method subject to the same strict sanitizer, size cap, and
       zero-host-path guarantees—never serving arbitrary host files.
     - **Attachments**: existing deduplicated allowlisted blobs.
     - **Links**: additive `item_links(source_id, relation, target_id, label)`
       tracing `producer → output → consumer` / `caused_by` / `depends_on`
       relationships as clickable in-world jump links (zero network).
   - **Explicit unavailability**: missing, redacted, size-capped, disallowed,
     or `--no-attachments`-omitted content must render a typed unavailability
     state (`omitted`, `disallowed-media-type`, `redacted-by-policy`,
     `unavailable`) and never invent placeholder data.
3. **`ACT` — separately authorized action surface (reserved, out of #202 v1)**:
   - Future operational actions (e.g., open in IDE, retry a stage, approve a
     gate) require an explicit opt-in, separately authorized host-controlled
     boundary—never implicit filesystem writes or browser-triggered mutation
     routes.
   - #202 v1 enforces read-only `GET`/`HEAD` only (`405` on all mutation
     methods) and zero action endpoints.
