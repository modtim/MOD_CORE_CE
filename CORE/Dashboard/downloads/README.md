# Downloads Directory

This directory contains the built MOD Platform Tray app installers.

## Contents

**Nothing is published here yet.** This directory is empty except this README: no
`manifest.json`, no artifact. `downloads.js` renders "Not yet available" per platform
until a manifest with that platform's key exists — nothing unbuilt is ever advertised.

When artifacts do ship, this directory will contain:

- `MOD-Setup-<version>.msi` - Windows installer (WIN.1.s2, MSI via WiX; `manifest.files.windows`)
- `MODTray-<version>.exe` - Windows tray app (`manifest.files.windows_app`)
- `*.pkg` - macOS installer (MAC.1.s5; `manifest.files.macos`)
- `*.AppImage` - Linux universal executable (`manifest.files.linux_appimage`)
- `*.deb` - Debian/Ubuntu package (`manifest.files.linux_deb`)
- `manifest.json` - Download manifest for the web page

The Windows keys mirror the release-bundle `windows_artifacts` entries
(`CORE/release/build-bundle.sh:502` `dashboard_key` = `windows` / `windows_app`,
WIN.1.s15); the bundle manifest is the source of truth, this one is the Dashboard's copy.

## How the artifacts get here

There is no tray-app build step in `start-main-server.sh` and no
`CORE/TrayApp/build-for-distribution.sh` — those instructions described a build that
was never wired up. The real path is the release-bundle channel (WIN.1.s15 / MAC.1.s5):

1. The platform artifacts are built on their own boxes:
   `CORE/release/windows-installer/build.sh` -> `MOD-Setup-<version>.msi` (needs a Windows box + WiX v4),
   `CORE/WindowsApp/build.sh` -> `MODTray-<version>.exe` (needs a Windows box),
   `CORE/release/macos-pkg/` -> `MOD-<version>.pkg` (needs a Mac).
2. `CORE/release/build-bundle.sh --windows-artifact <kind>=<path>` copies them into the bundle
   and writes `windows_artifacts` into the bundle `manifest.json` (WIN.1.s15).
3. That bundle manifest is what feeds this directory's `manifest.json` under the
   `files.windows` / `files.windows_app` keys the Dashboard reads (`downloads.js`).

Until step 1 has run on real toolchains, this directory stays empty and the page says
"Not yet available" — never a dead link, never a workaround.

## Accessing Downloads

After deployment, the installers are available at:

**Local:** `http://dashboard.localhost/downloads.html`
**Production:** `https://dashboard.yourdomain.com/downloads.html`

## File Serving

The nginx configuration in `Dashboard/nginx.conf` serves this directory:

```nginx
location /downloads/ {
    alias /usr/share/nginx/html/downloads/;
    autoindex on;
    add_header Cache-Control "public, max-age=3600";
}
```

Direct file access: `http://dashboard.localhost/downloads/filename.exe`

## Rebuild

To publish a new artifact version, re-run the platform build on its own box, then
re-bundle with `--windows-artifact installer=... --windows-artifact app=...`
(WIN.1.s15) and refresh this directory's `manifest.json`. There is no TrayApp npm
package to bump — `CORE/TrayApp/` is the retired Electron shell, not a build target.

Then rebuild the Dashboard container:

```bash
cd ..
docker-compose build mod-dashboard
docker-compose up -d mod-dashboard
```
