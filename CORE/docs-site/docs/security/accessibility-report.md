---
sidebar_position: 51
slug: /security/accessibility-report
title: Accessibility Conformance Report (VPAT 2.5)
description: MOD's Accessibility Conformance Report in W3C WAI VPAT 2.5 format for the Dashboard and Workflow Builder, with per-rule conformance levels backed by automated axe-core evidence, honest support levels, and a gap roadmap.
---

# Accessibility Conformance Report (VPAT 2.5)

> **How to read this report.** Every row states a conformance level backed by
> evidence we can point to: the automated axe-core checks in the E2E suite
> (`CORE/e2e/tests/a11y/axe-main-pages.spec.ts`) and the recorded baseline
> (`CORE/e2e/tests/a11y/axe-baseline.json`). Anything we have not verified
> ships as **Not Satisfied** or **Partially Satisfied** with a note, or is
> marked **Not evaluated**. MOD does **not** claim WCAG AA conformance across
> the board, and this report is not an independent audit.

## 1. Subject

| Field | Value |
|-------|-------|
| Name | MOD Dashboard and Workflow Builder (web applications) |
| Type | Website / authenticated web-application pages |
| Version | Current release; this report is refreshed each release |
| Last updated | 2026-11-03 |

## 2. Requirement summary

| Field | Value |
|-------|-------|
| Requirement | WCAG 2.1 |
| Support level targeted | Level AA |
| Community Edition (CE) | Best-effort only: the same automated checks run on CE, but no AA support level is claimed for CE |

## 3. Platform / user agent

| Field | Value |
|-------|-------|
| Platform | The apps are served to the tenant's own browser (modern evergreen browsers) |
| User agent tested | Chromium via Playwright (the E2E suite's browser) |
| Notes | No conformance claim is made for user agents or platforms not exercised by the checks below |

## 4. Assistive technology and testing method

- **Automated:** axe-core (via `@axe-core/playwright`) run against the authenticated Dashboard home/library/workers/settings pages and the Workflow Builder canvas page, with WCAG tags `wcag2a`, `wcag2aa`, `wcag21a`, `wcag21aa`. The suite fails on violations of `serious` or `critical` impact; known rule ids are exempted via a recorded per-page baseline.
- **Manual AT testing:** not yet performed. No screen-reader, magnification-only, or keyboard-only audit has been run. Rows that depend on manual assistive-technology testing are **Not evaluated**.
- **Limitations:** axe-core covers a subset of WCAG 2.1 success criteria. A green automated result is evidence that no serious/critical violation was *detected* for the checked rules on the tested pages — it is not proof of conformance.

## 5. Notes — per-rule conformance

| Page | Rule set / rule id | WCAG level | Conformance | Evidence / notes |
|------|--------------------|------------|-------------|------------------|
| Dashboard home | axe-checked WCAG 2.0/2.1 A + AA rules, serious/critical impact | 2.1 A, AA | Partially Satisfied | No serious/critical violations detected this release (page baseline is empty); minor/moderate impacts are not gated and were not assessed |
| Dashboard library | axe-checked WCAG 2.0/2.1 A + AA rules, serious/critical impact | 2.1 A, AA | Partially Satisfied | Same as above (page baseline is empty) |
| Dashboard workers | axe-checked WCAG 2.0/2.1 A + AA rules, serious/critical impact | 2.1 A, AA | Partially Satisfied | Same as above (page baseline is empty) |
| Dashboard settings | axe-checked WCAG 2.0/2.1 A + AA rules, serious/critical impact | 2.1 A, AA | Partially Satisfied | Same as above (page baseline is empty) |
| Workflow Builder canvas | `color-contrast` (WCAG 2.1 AA 1.4.3 / 1.4.8 / 1.4.3-a) | 2.1 AA | Not Satisfied | Known serious violation recorded in the per-page baseline; see gap roadmap (§6) |
| Workflow Builder canvas | other axe-checked WCAG 2.0/2.1 A + AA rules, serious/critical impact | 2.1 A, AA | Partially Satisfied | No new serious/critical violations beyond the baselined `color-contrast` this release |
| All tested pages | keyboard operation, visible focus, focus order (2.1 2.1.1–2.1.4, 2.4.7, 2.4.3) | 2.1 A, AA | Not evaluated | axe coverage is partial; no manual keyboard audit has been run |
| All tested pages | accessible names/roles for custom canvas and dashboard controls (manual verification) | 2.1 A, AA | Not evaluated | Automated name/role checks only cover what axe inspects |
| All tested pages | captions, alternatives for non-text media, motion, zoom, reflow (2.1 1.2.x, 1.3.x, 1.4.x) | 2.1 A, AA | Not evaluated | Not exercised by the automated checks; no manual AT audit |
| Pages not listed above (onboarding, admin surfaces, etc.) | all WCAG 2.1 criteria | 2.1 A, AA | Not evaluated | Not covered by the automated checks this release |

## 6. Gap roadmap (known axe rule ids → planned fix)

| Rule id | Page | Impact | Planned fix | Status |
|---------|------|--------|-------------|--------|
| `color-contrast` | Workflow Builder canvas | serious | Raise canvas UI text/edge contrast to meet WCAG 2.1 AA (1.4.3 minimum 4.5:1, 1.4.8 at 7:1) | Open — planned fix |

New `serious`/`critical` axe rule ids on any tested page fail the E2E build, so
this roadmap and the baseline are refreshed each release: the lead records new
known rule ids in `axe-baseline.json`, they appear here as Not Satisfied with a
planned fix, and only previously unknown rule ids keep failing the suite.

## 7. Report metadata

- **Author:** MOD Tech Labs Inc. — generated from the E2E a11y harness results.
- **Refresh policy:** re-evaluated each release alongside the axe run; baseline changes are recorded here.
- **Questions / audit requests:** use the tenant security contact and the incident-reporting
  process described on the [Incident Response](/security/incident-response) page.
