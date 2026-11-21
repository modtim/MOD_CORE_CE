# Linux worker tray (owner decision 10-08 Q3=A)

Linux worker desktops get a native tray in the Go worker, behind a build tag,
on the same code path as Windows/macOS. GUI deps only in desktop builds;
headless servers are unaffected.

## Same code path

| Piece | File | Tag |
|---|---|---|
| Menu model (status→icon, pause/resume label, tooltips) | `internal/tray/tray.go` | none (all platforms) |
| Event loop (sync tick, click routing, Quit teardown) | `internal/tray/menu.go` | none (all platforms) |
| Windows backend | `internal/tray/platform_windows.go` | `windows` |
| macOS backend | `internal/tray/platform_darwin.go` | `darwin && cgo` |
| Linux backend | `internal/tray/platform_linux.go` | `linux && tray` |
| Linux no-op stub | `internal/tray/platform_other.go` | `(!windows && (!darwin || !cgo)) && !(linux && tray)` |
| Worker hook (native) | `cmd/worker/tray_native.go` | `windows || darwin` |
| Worker hook (linux desktop) | `cmd/worker/tray_linux.go` | `linux && tray` |
| Worker hook (no-op) | `cmd/worker/tray_other.go` | `(!windows && !darwin) && !(linux && tray)` |

The Linux backend implements the same `MenuSink` seam the shared loop drives,
so nothing about the menu (status, pause/resume, open local UI, quit) is
Linux-specific: only the DBus plumbing in `platform_linux.go` is.

## Build tags

Default linux build — what the Makefile and Dockerfile ship
(`CGO_ENABLED=0 GOOS=linux`), and every headless server/container worker:

    go build ./cmd/worker                       # tray = no-op stub, zero GUI deps

Desktop linux build (opt-in):

    go build -tags tray ./cmd/worker            # tray enabled

`-tags tray` is the only switch. Nothing else changes: same binary name, same
pause gate, same local UI, same Quit path (it cancels the worker's root
context, like SIGTERM/SIGINT).

## Package deps

Build time: **none beyond the Go module graph** — `fyne.io/systray` v1.12.2
and its `github.com/godbus/dbus/v5` dependency are pure Go. The library's
linux backend speaks the StatusNotifierItem spec over the user's DBus session
bus and serves a `com.canonical.AppMenu` dropdown. It needs **no cgo** and
**no libayatana-appindicator** C library, so the tagged build links no system
GUI libraries. (The Electron TrayApp was deleted in `0de6e43ae` and is not
revived here.)

Runtime, on the desktop:

- KDE, modern XFCE/LXQt: works natively.
- Gnome: needs a StatusNotifierItem proxy — install `snixembed`
  ("StatusNotifierItems XEmbedded"), as the systray library recommends.
- No DBus session (headless, ssh-only, bare Wayland with no provider): the
  library logs one line to its own log, never receives clicks, and the worker
  is unaffected.

The tray also stays off when the worker detects a container
(`inContainerEnv()` — `/.dockerenv`, `MOD_IN_CONTAINER`, ...), same rule as
windows/darwin.

## Verifying

Unit tests for the shared menu/state logic need no display:

    go test ./internal/tray/...

Tagged linux build (proves the tray compiles for linux; no display needed):

    go vet -tags tray ./internal/tray/ ./cmd/worker
    go build -tags tray ./cmd/worker
