package tray

import (
	_ "embed"
)

// Embedded icon bytes. PNGs are used on darwin/unix; classic .ico files on
// Windows (the systray library feeds Windows' LoadImageW/IMAGE_ICON, which
// needs an .ico). They are small anti-aliased coloured circles:
//
//	ok       green   (#2FA05A)
//	degraded amber   (#E0A400)
//	error    red     (#D64545)
//	paused   gray    (#9AA0A6)
//
// They are produced by a small throwaway Go program (stdlib-only) that draws
// an anti-aliased filled circle in each colour and encodes it as PNG and as a
// classic 32-bit .ico; the output is committed here so no build-time tooling
// is needed to build the worker.
var (
	//go:embed icons/ok.png
	okPNG []byte
	//go:embed icons/degraded.png
	degradedPNG []byte
	//go:embed icons/error.png
	errorPNG []byte
	//go:embed icons/paused.png
	pausedPNG []byte
)

var (
	//go:embed icons/ok.ico
	okICO []byte
	//go:embed icons/degraded.ico
	degradedICO []byte
	//go:embed icons/error.ico
	errorICO []byte
	//go:embed icons/paused.ico
	pausedICO []byte
)

// iconPNG returns the PNG icon bytes for a health state.
func iconPNG(s HealthState) []byte {
	switch s {
	case StateOK:
		return okPNG
	case StateDegraded:
		return degradedPNG
	case StateError:
		return errorPNG
	case StatePaused:
		return pausedPNG
	}
	return okPNG
}

// iconICO returns the Windows .ico icon bytes for a health state.
func iconICO(s HealthState) []byte {
	switch s {
	case StateOK:
		return okICO
	case StateDegraded:
		return degradedICO
	case StateError:
		return errorICO
	case StatePaused:
		return pausedICO
	}
	return okICO
}
