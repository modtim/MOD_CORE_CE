#!/usr/bin/env node
/**
 * Emits CORE/Dashboard/js/vendor/assistant-nav.js from the canonical
 * CORE/WorkflowBuilder/lib/assistant-nav/navActions.ts source, for Dashboard's
 * plain-<script> (no bundler) runtime.
 *
 * Usage: node CORE/WorkflowBuilder/lib/assistant-nav/build-dashboard.js
 * (wired as `npm run build:assistant-nav` in CORE/WorkflowBuilder/package.json)
 *
 * How it works: tsc compiles navActions.ts to CommonJS (the only
 * classic-script-friendly emit tsc has); this script then wraps that
 * output in an IIFE that supplies a local `exports` object and assigns
 * the result to `window.ModAssistantNav`, so the compiled file can be
 * loaded with a plain `<script src="...">` tag — no module loader,
 * no bundler.
 */
"use strict";

const { execFileSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const SHARED_DIR = __dirname;
const BUILD_DIR = path.join(SHARED_DIR, ".dashboard-build");
const TSCONFIG = path.join(SHARED_DIR, "tsconfig.dashboard.json");
const COMPILED_JS = path.join(BUILD_DIR, "navActions.js");
const OUT_FILE = path.resolve(SHARED_DIR, "../../../Dashboard/js/vendor/assistant-nav.js");

const WB_TSC = path.join(SHARED_DIR, "../../node_modules/typescript/bin/tsc");
const tscBin = fs.existsSync(WB_TSC) ? WB_TSC : require.resolve("typescript/bin/tsc");

execFileSync(process.execPath, [tscBin, "-p", TSCONFIG], {
    stdio: "inherit",
});

const compiled = fs.readFileSync(COMPILED_JS, "utf8");

const banner =
    "// GENERATED FILE — do not edit by hand.\n" +
    "// Source: CORE/WorkflowBuilder/lib/assistant-nav/navActions.ts\n" +
    "// Regenerate: node CORE/WorkflowBuilder/lib/assistant-nav/build-dashboard.js\n" +
    "// (mod-assistant-agent §6.4 — shared nav-action runtime)\n";

const wrapped =
    banner +
    "(function (global) {\n" +
    '"use strict";\n' +
    "var exports = {};\n" +
    compiled +
    "\n" +
    "global.ModAssistantNav = exports;\n" +
    "})(typeof window !== \"undefined\" ? window : this);\n";

fs.mkdirSync(path.dirname(OUT_FILE), { recursive: true });
fs.writeFileSync(OUT_FILE, wrapped, "utf8");

fs.rmSync(BUILD_DIR, { recursive: true, force: true });

console.log("Wrote " + OUT_FILE);
