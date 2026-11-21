// R36.2 — sharp / @img/* (sharp-libvips is LGPL-3.0-or-later) must NOT ship in
// the WorkflowBuilder image. sharp is only an OPTIONAL dependency of next@15
// (next needs it for the /_image optimizer, which this app disables with
// images.unoptimized), so the Dockerfile strips it from the standalone output.
// This test is the guard: it fails if the lockfile ever makes sharp a REQUIRED
// dep of something we ship, or if the Dockerfile stops stripping it.
import * as fs from 'fs';
import * as path from 'path';

const DOCKERFILE = fs.readFileSync(path.resolve(__dirname, '../Dockerfile'), 'utf-8');
const NEXT_CONFIG = fs.readFileSync(path.resolve(__dirname, '../next.config.mjs'), 'utf-8');
const LOCK = JSON.parse(
  fs.readFileSync(path.resolve(__dirname, '../package-lock.json'), 'utf-8'),
);
const PACKAGES: Record<string, any> = LOCK.packages ?? {};

const bareName = (key: string) => key.replace(/^node_modules\//, '');
const isSharpish = (name: string) => bareName(name) === 'sharp' || bareName(name).startsWith('@img/');

describe('sharp is not shipped in the WorkflowBuilder image', () => {
  it('next.config.mjs disables the image optimizer (the only sharp consumer)', () => {
    expect(NEXT_CONFIG).toMatch(/images:\s*\{[^}]*unoptimized:\s*true/);
  });

  it('sharp, if pinned at all, is pinned optional', () => {
    const sharpEntry = PACKAGES['node_modules/sharp'];
    if (sharpEntry) {
      expect(sharpEntry.optional).toBe(true);
    }
  });

  it('nothing we ship requires sharp (only next, optionally)', () => {
    const requirers = Object.entries(PACKAGES).filter(([, pkg]) => {
      const deps = (pkg as { dependencies?: Record<string, string> }).dependencies ?? {};
      return Object.keys(deps).some(isSharpish);
    });
    // The only legal requirers are the sharp subtree itself (sharp -> @img/*).
    for (const [name] of requirers) {
      expect(isSharpish(name)).toBe(true);
    }
    const nextEntry = PACKAGES['node_modules/next'] as { optionalDependencies?: Record<string, string> };
    expect(Object.keys(nextEntry.optionalDependencies ?? {}).some(isSharpish)).toBe(true);
  });

  it('the Dockerfile strips sharp + @img from the shipped image', () => {
    expect(DOCKERFILE).toMatch(/RUN rm -rf \/app\/node_modules\/sharp \/app\/node_modules\/@img/);
  });

  it('the Dockerfile does not use a blanket --omit=optional (that would drop @next/swc-*)', () => {
    expect(DOCKERFILE).not.toMatch(/omit=optional/);
  });
});
