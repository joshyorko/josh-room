# Third-Party Notices and Attribution for Josh Room View

The Josh Room View offline HTML template embeds the following pinned open-source
runtime libraries as verified, self-contained blobs (zero runtime CDN or network
fetches):

- **Three.js** (`three@0.170.0`) — MIT License — Copyright © 2010–2024 three.js authors.
- **sql.js** (`sql.js@1.12.0`, SQLite compiled to WebAssembly) — MIT License — Copyright © 2012–2024 sql.js authors.
- **PDF.js** (`pdfjs-dist@4.10.38`) — Apache License 2.0 — Copyright © Mozilla Foundation.

## Referenced rendering techniques (original clean-room implementation)

The 3D UNIX filesystem landscape in `scene3d.js` and `layout.js` is written fresh
for Josh Room. Its visual proportions, shader techniques, and interaction design
acknowledge the following permissive open-source references and published research:

- Anti-aliased world-space `fwidth` grid shader technique inspired by Fyrestar's
  `THREE.InfiniteGridHelper` (MIT License) and `thebuilder/fsn` (MIT License).
- Apex-distance volumetric spotlight cone attenuation inspired by
  `jeromeetienne/threex.volumetricspotlight` (MIT License).
- Lifted-midpoint camera flight arcs inspired by `Dataverse-Vibe-Explorer` (MIT License)
  and published InfoVis literature (Mackinlay/Card/Robertson, Heer/Robertson,
  CodeCity 5-quantile binning, SpaceTree branch summaries).
- Historical SGI IRIX FSN numerical parameters (60° FOV, −70° initial tilt,
  0.6× unselected restraint, Grass palette, spacing ratios) are factual reference
  points only; no SGI binaries, icons, film assets, or GPL/LGPL/unlicensed code
  are included or redistributed.
