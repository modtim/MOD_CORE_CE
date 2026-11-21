# vendors: @loaders.gl (LAS/LAZ point-cloud preview)

Self-contained ESM bundles for the Dashboard point-cloud preview
(`js/file-preview.js` → `renderPointCloudPreview`, GSEC FX.19 R-042 /
EGRESS_INVENTORY row 3). Served at `/vendor/loaders.gl/*` — no public CDN
at runtime.

## Contents

| File        | Exports            | Size     |
|-------------|--------------------|----------|
| `core.esm.js` | `load`           | ~33 KB   |
| `las.esm.js`  | `LASLoader`      | ~225 KB  |

## Resolved versions (2026-10-03)

- `@loaders.gl/core` **4.5.2** (MIT)
- `@loaders.gl/las` **4.5.2** (MIT)
  - transitive: `@loaders.gl/loader-utils` 4.5.2, `@loaders.gl/schema`
    4.5.1, `@loaders.gl/schema-utils` 4.5.2, `laz-perf` ^0.0.6 (0.0.6
    installed, Apache-2.0 — dependency of the package but NOT in the
    bundle; see below)
- built with `esbuild` **0.28.2**

## Rebuild

```sh
# in any scratch dir (not the repo)
npm init -y
npm i @loaders.gl/core@4 @loaders.gl/las@4 esbuild
printf 'export { load } from "@loaders.gl/core";\n'        > entry-core.js
printf 'export { LASLoader } from "@loaders.gl/las";\n'    > entry-las.js
npx esbuild entry-core.js --bundle --format=esm --platform=browser \
    --minify --outfile=core.esm.js
npx esbuild entry-las.js  --bundle --format=esm --platform=browser \
    --minify --outfile=las.esm.js
```

Then overwrite the files here (no other files in this directory are
needed — see below).

## Self-containment / no-CDN notes

- **LAZ decompression is fully inlined.** The default `LASLoader`
  (LAS v1.2–1.4, incl. LAZ) in `@loaders.gl/las` v4 uses the
  `LAZPerfLoader` path, whose laz decoder is an Emscripten **asm.js
  SINGLE_FILE build inlined into the package source**
  (`@loaders.gl/las/src/libs/laz-perf/laz-perf.ts`). The only binary
  data URI in the bundle is the ~21 KB emscripten memory initializer —
  no `.wasm` file exists or is fetched at runtime, so there is nothing
  to copy next to these bundles.
- **The npm `laz-perf@0.0.6` package (with `laz-perf.wasm`) is NOT
  bundled.** It only backs the benchmark-only `LAZRsLoader` export,
  which the entry file does not import (esbuild tree-shakes it).
  Verified: `grep -c 'http' las.esm.js` → 0.
- **Workers are disabled by the caller.** `LASWorkerLoader.worker` is
  `true` by default, in which case `load()` would fetch its worker
  script from `unpkg.com`. `file-preview.js` therefore calls
  `load(url, LASLoader, { worker: false })` so parsing runs on the main
  thread with the inlined decoder. The two `https://unpkg.com/...`
  string literals remaining in `core.esm.js` are the
  `DEFAULT_LOADER_OPTIONS.CDN` constant and the worker-URL generator —
  both are dead code paths when `worker: false` is passed and are never
  fetched.
- **Bare imports:** none in either bundle (fully bundled ESM,
  `--format=esm`).

## License

- `@loaders.gl/*`: MIT (vis.gl / loaders.gl contributors).
- The inlined LAZ decoder is compiled from Howard Butler's
  `laz-perf` (https://github.com/hobu/laz-perf), which is **LGPL**;
  the in-file header in the package source carries the LGPL notice and
  build instructions. The separate `laz-perf` npm package (Apache-2.0)
  is not part of the bundle.
