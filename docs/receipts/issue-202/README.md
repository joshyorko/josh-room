# Issue #202 Synthetic Verification & Visual Review Gate Receipts

All screenshots and receipts in this directory were generated strictly from
deterministic synthetic fixtures (`josh-room view export --provider synthetic*`)
opened offline from `file://` in headless Google Chrome with all `http://` and
`https://` network requests intercepted and blocked. No real workspace data,
credentials, private paths, or third-party assets are included.

## Visual Review Gate Captures (1440×900)

| Capture | Theme | What it verifies |
| --- | --- | --- |
| [01-night-overview.png](01-night-overview.png) | Night (default) | Full-viewport SGI FSN landscape: foreground root `/` pedestal, glowing parent-child ground wires, fanned group/stage pedestals, upright 5-quantile file blocks, dark gradient sky + matching `FogExp2` + `fwidth` infinite grid, thin perimeter HUD, inspector hidden on load. |
| [02-night-directory.png](02-night-directory.png) | Night | Mid-flight / directory focus settled on `Sector Beta / Verify` (`20 items · 6 fail`) showing individual fail blocks with WCAG `×` crowns, pass blocks, and the `+9 more` SpaceTree summary tower. |
| [03-night-spotlight-v2.png](03-night-spotlight-v2.png) | Night | Selected `verify-01.fail` under **Spotlight v2**: `SpotLight.map` warm floor pool with soft PCF shadows, feathered `FrontSide` apex-faded dithered volumetric beam, dimmed surroundings/wires/grid, no literal lamp, intact coral block hue and `×` crown. |
| [04-night-spotlight-baseline.png](04-night-spotlight-baseline.png) | Night | **Spotlight A/B Baseline** on the exact same item/camera (`SpotLight(0xfff1c9, 300*L)` + additive floor decal + cylinder): demonstrates the additive pedestal white-out and sharper beam edge eliminated by Spotlight v2. |
| [03b-night-spotlight-v2-evidence-pdf.png](03b-night-spotlight-v2-evidence-pdf.png) | Night | Dismissible contextual Evidence drawer open alongside Spotlight v2, rendering multi-page synthetic `evidence-report.pdf` via bundled `pdf.js` (`p1 in 12.6 ms`, zero network). |
| [05-grass-overview.png](05-grass-overview.png) | Grass (FSN light) | FSN-inspired Grass palette (`#0087d5` → `#91fff0` sky, `#0f4f1a` → `#418a59` ground) with identical geometry and navigation. |
| [06-grass-directory.png](06-grass-directory.png) | Grass | Directory focus on `Sector Beta / Verify` in Grass theme. |
| [07-grass-spotlight-v2.png](07-grass-spotlight-v2.png) | Grass | **Spotlight v2** in Grass (`NormalBlending` beam + `SpotLight.map` warm pool, vivid block color intact). |
| [08-grass-spotlight-baseline.png](08-grass-spotlight-baseline.png) | Grass | **Spotlight A/B Baseline** in Grass on the exact same item/camera for side-by-side review. |
| [09-night-reduced-motion.png](09-night-reduced-motion.png) | Night | Selected item under `prefers-reduced-motion: reduce`: instant camera cut, no intro flight/tour, opacity spotlight fade preserved. |
| [10-sql-console.png](10-sql-console.png) | Night | Read-only SQL console running preset `All failures` (`53 rows`) in `sql.js` WASM over the embedded `room-view-v1` database. |
| [11-scale-10-items.png](11-scale-10-items.png) | Night | 10-item synthetic fixture (`synthetic-small`): spacious layout, constant world-unit block dimensions. |
| [12-scale-540-items.png](12-scale-540-items.png) | Night | 540-item synthetic fixture (`synthetic-large`): >50-item groups collapse into SpaceTree stage summary towers on overview while keeping constant world-unit geometry. |
| [13-webgl-off-grid-fallback.png](13-webgl-off-grid-fallback.png) | Night | Launched with `--disable-webgl`: automatic 2D Grid fallback (`300` tiles), attached evidence viewer, and SQL console all operational. |

## Spotlight v2 vs Baseline A/B Findings

Comparing `03-night-spotlight-v2.png` vs `04-night-spotlight-baseline.png` and
`07-grass-spotlight-v2.png` vs `08-grass-spotlight-baseline.png`:

- **Hue & pedestal preservation**: Spotlight v2 (`SpotLight.map` floor pool + `NeutralToneMapping`) preserves the warm ivory pool and coral/emerald block tones without clipping the pedestal surface to flat white, whereas Baseline stacks an additive floor decal over the lit pedestal and clips to `#ffffff`.
- **Beam softness**: Spotlight v2's `FrontSide` apex-faded, view-normal-attenuated, dithered cone blends smoothly without hard cylinder edges or stray wireframes in both Night and Grass.
- **First-click responsiveness**: Persistent `SpotLight` with precompiled shaders records `0 ms` click-handler stall and `1` shadow-map update on move (0 during idle).
- **Decision**: Retain **Spotlight v2** as the default and keep **Baseline** toggleable (`Spot: base` / `?spotlight=baseline`) for review comparison.

## Recorded Machine Receipt

Exact metrics are saved in [browser-verification-receipt.json](browser-verification-receipt.json).
Key verified results on `synthetic` (300 items, 1.57 MiB offline HTML) and `synthetic-large` (540 items):

- Zero external network fetches (`blocked_external_requests: []`), `0` CSP violations, `0` console errors.
- Time to first interactive 3D frame: `1233.9 ms` (300 items) / `106.9 ms` (540 items) — within `< 2000 ms` budget.
- Render on demand: `0` renders and `0` shadow updates over `1200 ms` idle.
- PDF.js first page visible: `12.6 ms` (`< 500 ms` budget).
- Inert HTML report security probe: `window.__JOSH_ROOM_HTML_ATTACHMENT_XSS__` never executed (`scripts blocked: 1`).
- Read-only SQL console: preset returned `53` rows; `DROP TABLE items;` rejected.
- WebGL-off fallback: automatic switch to 2D Grid (`300` tiles) with Evidence drawer and SQL active.

## Pending Human / Non-Automated Acceptance Criteria

- **Five-second recognition test** with 8–10 unbriefed human participants (≥70% identifying a 3D filesystem/directory tree).
- **Three-task human timing comparison** between 3D navigation and 2D Grid (times, error counts, and participant preference).
- **Low-end GPU hardware timing check** (current headless Chrome receipt used ANGLE SwiftShader software WebGL).
