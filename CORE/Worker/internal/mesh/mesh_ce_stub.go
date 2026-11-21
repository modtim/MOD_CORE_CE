// CE stub for
// the DENIED CORE/Worker/internal/mesh package (open-core/manifest.yaml deny
// glob "CORE/Worker/internal/mesh/**").
//
// heartbeat.go is shared code that ships in CE and names three symbols from that
// package (the SiteMesh interface's `*mesh.Status` / `*mesh.Directive` and the
// foreign-tailscaled probe). Without this stub the CE Go worker does not compile
// (the embedded worker is built from source: CORE/Worker/Makefile).
//
// Community Edition ships NO site-tailnet client: the real package (docker /
// foreign_tailscaled / legacy / managed / site + tests) stays denied, so the
// worker never joins a site tailnet and never reports site_mesh. This file
// carries no paid logic, no licence gate and no quantity cap; it only keeps the
// shared heartbeat code compiling.
package mesh

// Status is the site-tailnet status a reconciler would report. CE never
// reconciles one, so the type exists only to satisfy the heartbeat wire shape.
type Status struct{}

// Directive is the manager's site-tailnet directive (login server + epoch +
// single-use key). CE ignores it: the field is parsed and dropped.
type Directive struct{}

// ForeignTailscaled reports host warnings about a foreign tailscaled owning
// the kernel tailscale0. CE runs no tailscaled, so there is nothing to report.
func ForeignTailscaled() []string { return []string{} }
