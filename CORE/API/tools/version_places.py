#!/usr/bin/env python3
"""Every place that carries the MOD product/release version, and its value.

ALL-ONE-VERSION-1-1-0 (owner Tim 11-25): the product, API, Dashboard, Workflow
Builder, docs site, Helm charts, startup env examples, the worker and (owner
11-25 option A) the SDKs, tray, mobile, capture app and license-server UIs all carry
ONE release number. This module is the registry; the single pin test
CORE/API/tests/test_all_one_version.py fails when any place drifts.

    python3 CORE/API/tools/version_places.py          # print every place + value
    python3 CORE/API/tools/version_places.py --check  # rc 1 if any place differs

Source of truth: CORE/Dashboard/package.json "version". The worker file
CORE/Worker/VERSION carries a leading "v" ("v1.1.0"): the Go release code
(releases.parseSemver) and the API (_version_tuple, worker_version_gate) both
accept an optional "v", and the heartbeat string compare in
worker_management is exact-string against the published manifest, so the
file keeps the "v" form it has always had; it compares equal after trimming "v".

NOT in this registry on purpose: third-party image/package pins, API schema
versions (/v1), DB migration ids, protocol/token versions (mod-trigger-call-v1),
worker FEATURE floors (MIN_HOSTEXEC_AGENT_VERSION, FILE_SECRETS_MIN_VERSION,
Q21_MIN_AGENT_VERSION: minimums, not the release number) and test fixtures.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

CORE = Path(__file__).resolve().parents[2]      # .../CORE
REPO = CORE.parent                               # repo root (deploy/ and MOBILE/ live here)
SOURCE = ("CORE/Dashboard/package.json", r'"version":\s*"([^"]+)"')

# (path relative to repo root, regex with ONE group, expected match count, note)
PLACES = [
    ("CORE/Dashboard/package.json", r'^\s*"version":\s*"([^"]+)"', 1, "dashboard package"),
    ("CORE/Dashboard/package-lock.json", "json:version,packages..version", 2, "dashboard lock (root + packages root)"),
    ("CORE/Dashboard/js/version.js", r"PRODUCT_VERSION\s*=\s*'([^']+)'", 1, "dashboard stamp"),
    ("CORE/Dashboard/pages/dashboard-shell.html", r'id="shell-product-version"[^>]*>v([0-9.]+)<', 1, "shell fallback"),
    ("CORE/Dashboard/downloads.html", r'id="version">v([0-9.]+)<', 1, "downloads badge"),
    ("CORE/API/main.py", r'"PRODUCT_VERSION",\s*"([^"]+)"', 2, "FastAPI app version + /v1/version"),
    ("CORE/API/utils/license_client.py", r'"PRODUCT_VERSION",\s*"([^"]+)"', 1, "license client default"),
    ("CORE/API/utils/registry_credentials_client.py", r"'PRODUCT_VERSION',\s*'([^']+)'", 1, "registry UA default"),
    ("CORE/startup/.env.example", r'^PRODUCT_VERSION="([^"]+)"', 1, "env example"),
    ("CORE/startup/.env.public.example", r'^PRODUCT_VERSION="([^"]+)"', 1, "public env example"),
    ("CORE/WorkflowBuilder/package.json", r'^\s*"version":\s*"([^"]+)"', 1, "workflow builder package"),
    ("CORE/WorkflowBuilder/package-lock.json", "json:version,packages..version", 2, "workflow builder lock (root + packages root)"),
    ("CORE/docs-site/package.json", r'^\s*"version":\s*"([^"]+)"', 1, "docs site package"),
    ("CORE/docs-site/package-lock.json", "json:version,packages..version", 2, "docs site lock (root + packages root)"),
    ("CORE/deploy/k8s/mod/Chart.yaml", r'^version:\s*"([^"]+)"', 1, "chart version"),
    ("CORE/deploy/k8s/mod/Chart.yaml", r'^appVersion:\s*"([^"]+)"', 1, "chart appVersion"),
    ("deploy/helm/mod/Chart.yaml", r'^version:\s*"([^"]+)"', 1, "helm chart version"),
    ("deploy/helm/mod/Chart.yaml", r'^appVersion:\s*"([^"]+)"', 1, "helm chart appVersion"),
    ("CORE/LicenseServer/k8s/license-server/api-deployment.yaml",
     r'name:\s*PRODUCT_VERSION\s*\r?\n\s*value:\s*"([^"]+)"', 1, "license-server deployment env"),
    ("CORE/Worker/VERSION", r"^v?([0-9][^\s]*)\s*$", 1, "worker release (file carries leading v)"),
    # ALL-ONE-VERSION-SIDECAR-1-1-0 (owner Tim 11-25, option A): the mod-model-cache
    # sidecar (CORE/DockerContainers/mod-model-cache) carries the SAME number now
    # (was 0.2.1). Its compose image tag stays `mod-model-cache:dev` (NOT version-
    # derived; the only Version-derived tag is the worker's MOD_EGRESS_PROXY_IMAGE
    # for mod-egress-proxy, a different image), so no consumer can pull a tag that
    # does not exist; the bump needs an image rebuild only (docker build of the
    # sidecar context), no compose/tag change.
    ("CORE/DockerContainers/mod-model-cache/app.py", r'version="([^"]+)"', 1, "sidecar FastAPI app version"),
    ("CORE/DockerContainers/mod-model-cache/bundle.py", r'^CACHE_VERSION = "([^"]+)"', 1, "sidecar bundle cache_version"),
    # ALL-ONE-VERSION-PACKAGES-1-1-0 (owner Tim 11-25, option A): every separately
    # published / first-party package carries the same number too.
    ("CORE/SDK/python/pyproject.toml", r'^version\s*=\s*"([^"]+)"', 1, "python SDK package"),
    ("CORE/SDK/python/mod_core_sdk/__init__.py", r'^__version__\s*=\s*"([^"]+)"', 1, "python SDK __version__"),
    ("CORE/SDK/typescript/package.json", r'^\s*"version":\s*"([^"]+)"', 1, "typescript SDK package"),
    ("CORE/TrayApp/package.json", r'^\s*"version":\s*"([^"]+)"', 1, "tray app package (was 0.0.0-dev)"),
    ("CORE/WindowsApp/MODTray.csproj", r"<Version>([^<]+)</Version>", 1, "windows tray assembly version"),
    ("CORE/MODCaptureApp/ui/pubspec.yaml", r"^version:\s*([0-9.]+)\+\d+\s*$", 1, "capture app (build number after + not checked)"),
    ("MOBILE/package.json", r'^\s*"version":\s*"([^"]+)"', 1, "mobile package"),
    ("MOBILE/package-lock.json", "json:version,packages..version", 2, "mobile lock (root + packages root)"),
    ("MOBILE/android/app/build.gradle", r"modVersionName\s*:\s*\"([^\"]+)\"", 1, "mobile android versionName default"),
    ("CORE/LicenseServer/AdminDashboard/package.json", r'^\s*"version":\s*"([^"]+)"', 1, "license-server admin dashboard"),
    ("CORE/LicenseServer/UserPortal/package.json", r'^\s*"version":\s*"([^"]+)"', 1, "license-server user portal"),
    ("CORE/DockerContainers/ReactEmail/package.json", r'^\s*"version":\s*"([^"]+)"', 1, "react-email package"),
    ("CORE/SmokeTests/playwright/package.json", r'^\s*"version":\s*"([^"]+)"', 1, "smoke-test package"),
    ("CORE/SmokeTests/playwright/package-lock.json", "json:version,packages..version", 2, "smoke-test lock (root + packages root)"),
    # ALL-ONE-VERSION-1-1-0 FIX2 (lane r9700-1, 11-25): the FastAPI SERVICE apps
    # (license server, the store API, platform-monitor) carry the same number now;
    # each reads PRODUCT_VERSION env with a 1.1.0 default.
    ("CORE/LicenseServer/API/main.py", r'PRODUCT_VERSION",\s*"([^"]+)"', 2, "license-server FastAPI app version + root body"),
    ("CORE/Marketplace/API/main.py", r'PRODUCT_VERSION",\s*"([^"]+)"', 2, "store API FastAPI app version + root body"),
    ("CORE/PlatformMonitor/main.py", r'PRODUCT_VERSION",\s*"([^"]+)"', 1, "platform-monitor FastAPI app version (healthz reads app.version)"),
    ("CORE/LicenseServer/.env.example", r'^SERVICE_VERSION=([^#\s]+)', 1, "license-server env example"),
    ("CORE/LicenseServer/API/services/audit_service.py", r'SERVICE_VERSION",\s*"([^"]+)"', 1, "audit service default"),
    # ALL-ONE-VERSION-1-1-0 FIX3 (lane r9700-1, 11-25): the CE overlay AI-text
    # files. Each regex matches ONLY the CURRENT-version statement (the FACTS
    # row, the AGENTS/llms.txt product line) and NOT the historical release
    # chain in the README Status line (v1.0.2 / v1.0.1 / v1.0.0 are listed
    # there on purpose). README.md and README.md.tmpl stay in sync through
    # open-core/render_presentation.py (--check must keep reporting 0 differ).
    ("open-core/overlay/README.md", r'^\| Product version \| ([0-9.]+) \|', 1, "overlay README FACTS row"),
    ("open-core/overlay/README.md.tmpl", r'^\| Product version \| ([0-9.]+) \|', 1, "overlay README template FACTS row"),
    ("open-core/overlay/AGENTS.md", r'^- Product version ([0-9.]+):', 1, "overlay AGENTS product line"),
    ("open-core/overlay/llms.txt", r'^- Product version: ([0-9.]+) \(', 1, "overlay llms product line"),
]

# ---------------------------------------------------------------------------
# RELEASE-NOTES LAYER (ALL-ONE-VERSION-1-1-0 FIX3, lane r9700-1, 11-25)
#
# ONE SWITCH controls this whole group. The release notes for the current
# number are written separately from the code bump (owner rule, see
# the internal design notes item
# 6): CHANGELOG section, RELEASE-NOTES title, the presentation VERSION slug,
# the bug-report placeholder, the install guide.
#
#   RELEASE_NOTES_WRITTEN = False  -> the group is EXPECTED-STALE: `--check`
#       prints each entry flagged `STALE*` and does NOT count it as a
#       mismatch; the pin test reports it and passes.
#   RELEASE_NOTES_WRITTEN = True   -> the group is ENFORCED exactly like the
#       places above (set it the turn the 1.1.0 notes land; until then a
#       drift here is not a launch blocker).
# ---------------------------------------------------------------------------
RELEASE_NOTES_WRITTEN = False

RELEASE_NOTES_PLACES = [
    # the TOP section only (the historical sections below are not the release
    # number): the first `## [x.y.z]` heading after the file's title line.
    ("open-core/overlay/CHANGELOG.md", r'^# Changelog[\s\S]*?^## \[([0-9.]+)\]', 1, "changelog top section"),
    ("open-core/overlay/RELEASE-NOTES.md", r'^# MOD Core CE ([0-9.]+)$', 1, "release-notes title"),
    ("open-core/overlay/docs/assets/presentation.config.yaml", r'^\s*VERSION: \{value: "([0-9.]+)"\}', 1, "presentation VERSION slug"),
    ("open-core/overlay/.github/ISSUE_TEMPLATE/bug_report.yml", r'placeholder: "([0-9.]+)"', 1, "bug-report version placeholder"),
    ("open-core/overlay/docs/install-guide.md", r'MOD Core CE ([0-9.]+)', 1, "install guide release line (add it when the switch flips: none in the guide today)"),
]


def _read(rel):
    """Read a repo-relative file. The pytest harness mounts the CORE/ subtree at
    "/" (so CORE/Dashboard is /Dashboard), hence the CORE/-stripped fallback;
    open-core/overlay files live under the run tree's open-core/ dir, which sits
    next to this module's parent dirs, so every parent of this file is tried too
    (proven: /app/open-core is mounted in the pytest container)."""
    cands = [REPO / rel]
    if rel.startswith("CORE/"):
        cands.append(CORE / rel[5:])
    for p in Path(__file__).resolve().parents:
        cands.append(p / rel)
    for p in cands:
        if p.is_file():
            return p.read_text(encoding="utf-8")
    return None


def source_version():
    return json.loads(_read(SOURCE[0]))["version"]


def _scan(rel, rx, count):
    text = _read(rel)
    if text is None:
        return None
    if rx.startswith("json:"):      # lockfiles: only the two ROOT entries, not deps
        doc = json.loads(text)
        return [doc["version"], doc["packages"][""]["version"]]
    return re.findall(rx, text, re.M)


def collect():
    """Yield (path, note, [values], expected_count) for every enforced place."""
    for rel, rx, count, note in PLACES:
        yield rel, note, _scan(rel, rx, count), count


def collect_release_notes():
    """The release-notes layer (see the RELEASE_NOTES_WRITTEN switch above)."""
    for rel, rx, count, note in RELEASE_NOTES_PLACES:
        yield rel, note, _scan(rel, rx, count), count


def main(argv):
    want = source_version()
    bad = 0
    for rel, note, vals, count in collect():
        if vals is None:
            shown, ok = "(file not in this tree)", True   # charts live outside CORE/
        else:
            shown = ",".join(vals) or "(no match)"
            ok = len(vals) == count and all(v == want for v in vals)
        bad += 0 if ok else 1
        print(f"{'ok ' if ok else 'BAD'} {rel:62} {shown:12} {note}")
    stale = []
    for rel, note, vals, count in collect_release_notes():
        shown = ",".join(vals) if vals is not None else "(file not in this tree)"
        ok = vals is not None and len(vals) == count and all(v == want for v in vals)
        if RELEASE_NOTES_WRITTEN:
            bad += 0 if ok else 1
            print(f"{'ok ' if ok else 'BAD'} {rel:62} {shown:12} {note} [release-notes layer, ENFORCED]")
        else:
            print(f"{'ok ' if ok else 'STALE*'} {rel:62} {shown:12} {note} "
                  "[release-notes layer: EXPECTED-STALE until the notes for "
                  f"{want} are written; set RELEASE_NOTES_WRITTEN = True in "
                  "CORE/API/tools/version_places.py when they land]")
            if not ok:
                stale.append(rel)
    print(f"source of truth CORE/Dashboard/package.json = {want}; "
          f"mismatches = {bad}; release-notes layer = "
          f"{'ENFORCED' if RELEASE_NOTES_WRITTEN else 'EXPECTED-STALE'} "
          f"({len(stale)} flagged: {', '.join(stale) or 'none'})")
    return 1 if (bad and "--check" in argv) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
