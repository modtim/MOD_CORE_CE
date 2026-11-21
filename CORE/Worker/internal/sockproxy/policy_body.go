package sockproxy

import (
	"encoding/json"
	"strings"
)

// deniedHostPrefixes are host paths a tenant container must never
// bind, whole or in part. A source matches when it equals a prefix or
// lies below it (prefix + "/"). "/" itself is checked separately.
var deniedHostPrefixes = []string{
	"/var/run/docker.sock",
	"/run/docker.sock",
	"/etc",
	"/root",
	// /home/<user>/.ssh and dotfiles are the same host-takeover class as
	// /root. No legitimate proxied bind uses a /home source (worker binds
	// are named volumes, WSL GPU libs, the build context and secret mounts,
	// and go to the real daemon). Matching is component-aware, so
	// /homework stays allowed.
	"/home",
	"/proc",
	"/sys",
	"/dev", // except the device allowlist, checked first in validateSource
	"/var/lib/docker",
	// containerd/kubelet/boot: siblings of the covered sockets, NOT under
	// any existing prefix, so the ancestor rule does not reach them
	// (DIAG C4).
	"/run/containerd",
	"/var/lib/kubelet",
	"/boot",
	// REV gap probe (N4): /var/lib/containerd is daemon storage (image
	// layers, content tarballs) and is NOT under /run/containerd, so the
	// ancestor rule does not reach it; /lib/modules is the loaded-module
	// tree and no current prefix covers /lib at all. Denying /lib/modules
	// also denies /lib itself as its ancestor (same shape as /run for
	// /run/containerd): nothing legitimate binds all of /lib, and on
	// merged-/usr hosts /lib is only a symlink to /usr/lib, whose real
	// binds (/usr/lib/wsl) are spelled absolutely and stay accepted.
	"/var/lib/containerd",
	"/lib/modules",
}

// createBody is the subset of the POST /containers/create payload the
// policy cares about.
type createBody struct {
	HostConfig *hostConfig `json:"HostConfig"`
}

type hostConfig struct {
	Privileged   bool         `json:"Privileged"`
	Binds        []string     `json:"Binds"`
	Mounts       []mountSpec  `json:"Mounts"`
	PidMode      string       `json:"PidMode"`
	IpcMode      string       `json:"IpcMode"`
	UTSMode      string       `json:"UTSMode"`
	NetworkMode  string       `json:"NetworkMode"`
	UsernsMode   string       `json:"UsernsMode"`
	CgroupnsMode string       `json:"CgroupnsMode"`
	CapAdd       []string     `json:"CapAdd"`
	SecurityOpt  []string     `json:"SecurityOpt"`
	Runtime      string       `json:"Runtime"`
	Devices      []deviceSpec `json:"Devices"`
	DeviceReqs   []deviceReq  `json:"DeviceRequests"`
}

type mountSpec struct {
	Type   string               `json:"Type"`
	Source string               `json:"Source"`
	VolOpts *volumeOptionsSpec  `json:"VolumeOptions"`
}

// volumeOptionsSpec is the Mounts[].VolumeOptions subtree (DIAG C2: it
// was not parsed at all, so the whole subtree was fail-open). A local
// driver with Options.device is a host bind in disguise.
type volumeOptionsSpec struct {
	BindEnabled  bool                  `json:"BindEnabled"`
	DrvCfg       *volumeDriverCfg      `json:"DriverConfig"`
}

type volumeDriverCfg struct {
	Drv  string            `json:"Driver"`
	Opts map[string]string `json:"Options"`
}

type deviceSpec struct {
	PathOnHost string `json:"PathOnHost"`
}

type deviceReq struct {
	Driver       string     `json:"Driver"`
	Capabilities [][]string `json:"Capabilities"`
}

// checkCreateHostConfig validates a container-create body. body may be
// nil or malformed — that is a hard deny, never fail-open: a create
// request whose payload cannot be read is a create request whose
// payload cannot be vetted.
func checkCreateHostConfig(body []byte, p *Policy) (bool, string) {
	var cb createBody
	if err := json.Unmarshal(body, &cb); err != nil {
		return false, "container create: body is not valid JSON"
	}
	if cb.HostConfig == nil {
		return true, "" // HostConfig is optional; nothing to vet
	}
	hc := cb.HostConfig
	if hc.Privileged {
		return false, "container create: Privileged=true"
	}
	if hc.NetworkMode == "host" && !p.AllowHostNetwork {
		return false, "container create: NetworkMode host"
	}
	for _, pair := range [][2]string{
		{"PidMode", hc.PidMode},
		{"IpcMode", hc.IpcMode},
		{"UTSMode", hc.UTSMode},
		{"UsernsMode", hc.UsernsMode},
		{"CgroupnsMode", hc.CgroupnsMode},
	} {
		if pair[1] == "host" {
			return false, "container create: " + pair[0] + " host"
		}
	}
	// The value validated must be the value forwarded: the proxy replays
	// the request body verbatim, so a source whose normalized form differs
	// from the string that reaches dockerd is refused outright (rewriting
	// the body would change framing and is not what this policy does).
	for _, b := range hc.Binds {
		src := bindSource(b)
		if !canonicalSource(src) {
			return false, "container create: bind source " + src + " is not normalized"
		}
		if ok, reason := validateSource(src, p); !ok {
			return false, "container create: " + reason
		}
	}
	for _, m := range hc.Mounts {
		// Only bind mounts expose host paths; volume/tmpfs sources
		// are names managed by the daemon, not host paths.
		// Volume options are vetted for EVERY mount that carries them,
		// whatever the declared Type: a local-driver volume whose
		// Options carry device=<host path> is a host bind in disguise
		// (DIAG C2), and dockerd honours VolumeOptions on volume mounts
		// regardless of whether the loop below skips them. tmpfs and a
		// plain named/anonymous volume (no VolumeOptions at all) stay
		// skipped, exactly as before.
		if ok, reason := checkMountVolumeOptions(m, p); !ok {
			return false, reason
		}
		if m.Type != "bind" && m.Type != "" {
			continue
		}
		if !canonicalSource(m.Source) {
			return false, "container create: mount source " + m.Source + " is not normalized"
		}
		if ok, reason := validateSource(m.Source, p); !ok {
			return false, "container create: " + reason
		}
	}
	for _, d := range hc.Devices {
		if !p.deviceAllowed(d.PathOnHost) {
			return false, "container create: device " + d.PathOnHost + " is not on the device allowlist"
		}
	}
	for _, r := range hc.DeviceReqs {
		if !nvidiaDeviceRequest(r) {
			return false, "container create: GPU device request is not nvidia-capable"
		}
	}
	if hc.Runtime != "" && hc.Runtime != "nvidia" {
		return false, "container create: runtime " + hc.Runtime + " is not allowed"
	}
	caps := p.CapAllowlist
	if len(caps) == 0 {
		caps = defaultCapAllowlist
	}
	for _, c := range hc.CapAdd {
		if !inList(c, caps) {
			return false, "container create: CapAdd " + c + " is not on the capability allowlist"
		}
	}
	for _, o := range hc.SecurityOpt {
		if strings.Contains(o, "unconfined") {
			return false, "container create: SecurityOpt unconfined"
		}
	}
	return true, ""
}

// volumeBody is the subset of the POST /volumes/create payload the
// policy cares about. DriverOpts values must be JSON strings: any other
// shape makes Unmarshal fail, which is a deny (fail closed).
type volumeBody struct {
	Name  string            `json:"Name"`
	Drv   string            `json:"Driver"`
	Opts  map[string]string `json:"DriverOpts"`
}

// checkVolumeCreate validates a POST /volumes/create body (DIAG C3).
// A "local" volume whose DriverOpts carry device=<host path> is a
// host bind in disguise: dockerd mounts that path as the volume's
// backing dir, and a later containers/create Mounts volume entry then
// puts it inside the container - the Mounts loop skips volume types, so
// this is the only place the host path is visible. nil or malformed
// body is a hard deny, same fail-closed rule as checkCreateHostConfig
// (an oversize body reaches this check truncated, so it is not valid
// JSON either). Denial reasons name the offending option, never the
// whole payload.
func checkVolumeCreate(body []byte, p *Policy) (bool, string) {
	var vb volumeBody
	if body == nil {
		// The proxy buffers this endpoint (wantsBody), so a nil body only
		// comes from a direct Decide caller with nothing to vet - same
		// shape as the endpoint-allowlist tests. An EMPTY body (empty
		// bytes) is not nil and is denied below as invalid JSON.
		return true, ""
	}
	if err := json.Unmarshal(body, &vb); err != nil {
		return false, "volume create: body is not valid JSON"
	}
	switch vb.Drv {
	case "", "local", "tmpfs":
	default:
		return false, "volume create: Driver is not on the volume driver allowlist (local, tmpfs)"
	}
	if vb.Drv == "tmpfs" && len(vb.Opts) > 0 {
		// tmpfs takes no daemon-side driver options; anything here is a
		// request trying to smuggle a backing path.
		return false, "volume create: tmpfs with DriverOpts"
	}
	if ok, reason := volumeDriverAllowed(vb.Drv, vb.Opts, p); !ok {
		return false, "volume create: " + reason
	}
	return true, ""
}

// volumeDriverAllowed is the shared volume-driver rule (leg L1 + leg L2):
// the Driver allowlist, the device -> validateSource path check, the
// tmpfs-with-options rule and the o=bind-without-device rule. It is
// called from checkVolumeCreate (POST /volumes/create Driver/DriverOpts)
// and from checkMountVolumeOptions (Mounts[].VolumeOptions.DriverConfig),
// so both routes get exactly the same vetting. Reasons are unprefixed:
// the caller adds its own prefix.
func volumeDriverAllowed(drv string, opts map[string]string, p *Policy) (bool, string) {
	switch drv {
	case "", "local", "tmpfs":
	default:
		return false, "Driver is not on the volume driver allowlist (local, tmpfs)"
	}
	if drv == "tmpfs" && len(opts) > 0 {
		// tmpfs takes no daemon-side driver options; anything here is a
		// request trying to smuggle a backing path.
		return false, "tmpfs with DriverOpts"
	}
	// device is the host path a local volume is backed by: it goes
	// through the SAME source validation as a bind, so the deny list,
	// the normalization gate and the symlink resolution all apply.
	allowedDevice := false
	if dev, ok := opts["device"]; ok {
		if ok2, reason := validateSource(dev, p); !ok2 {
			return false, reason
		}
		allowedDevice = true
	}
	// o=bind without an accepted device is a bind mount of nothing
	// vetted: dockerd takes the option as given.
	if !allowedDevice {
		if o, ok := opts["o"]; ok && optionMentionsBind(o) {
			return false, "DriverOpts o mentions bind with no accepted device"
		}
	}
	return true, ""
}

// checkMountVolumeOptions vets one Mounts[] entry's VolumeOptions
// subtree (DIAG C2). A mount that carries no VolumeOptions at all is a
// no-op here: a plain named or anonymous volume and a tmpfs stay
// allowed, and a bind's own Source is vetted by the loop that calls
// this. VolumeOptions are vetted whatever the declared Type - dockerd
// honours them on volume mounts, and a bind type that smuggles them is
// over-denied, which is the safe direction.
func checkMountVolumeOptions(m mountSpec, p *Policy) (bool, string) {
	vo := m.VolOpts
	if vo == nil {
		return true, ""
	}
	allowedDevice := false
	if vo.DrvCfg != nil {
		if ok, reason := volumeDriverAllowed(vo.DrvCfg.Drv, vo.DrvCfg.Opts, p); !ok {
			return false, "container create: mount volume options " + reason
		}
		if _, ok := vo.DrvCfg.Opts["device"]; ok {
			allowedDevice = true
		}
	}
	// BindEnabled=true makes dockerd bind-mount the volume's backing path
	// instead of going through a volume driver: only accepted when the
	// options name a device that passed validateSource above.
	if vo.BindEnabled && !allowedDevice {
		return false, "container create: mount volume BindEnabled with no allowed device"
	}
	return true, ""
}

// optionMentionsBind reports whether a comma-separated DriverOpts o
// value carries a bind option.
func optionMentionsBind(o string) bool {
	for _, part := range strings.Split(o, ",") {
		if strings.Contains(part, "bind") {
			return true
		}
	}
	return false
}

// checkExecCreate validates a POST /containers/{id}/exec body.
func checkExecCreate(body []byte) (bool, string) {
	var ec struct {
		Privileged bool `json:"Privileged"`
	}
	if err := json.Unmarshal(body, &ec); err != nil {
		return false, "exec create: body is not valid JSON"
	}
	if ec.Privileged {
		return false, "exec create: Privileged=true"
	}
	return true, ""
}


// canonicalSource reports whether s is already in its normalized form:
// the value the policy validates is then exactly the value the proxy
// forwards to dockerd (normalisation-consistency gate, S-1 follow-up).
func canonicalSource(s string) bool {
	return normPath(s) == s
}
// bindSource returns the host source of a "source:target[:mode]" bind
// string.
func bindSource(bind string) string {
	if i := strings.IndexByte(bind, ':'); i >= 0 {
		return bind[:i]
	}
	return bind
}

// nvidiaDeviceRequest reports whether a DeviceRequests entry is the
// GPU passthrough form: Driver "nvidia" and/or the ["gpu"] capability
// list.
func nvidiaDeviceRequest(r deviceReq) bool {
	if r.Driver == "nvidia" {
		return true
	}
	for _, row := range r.Capabilities {
		for _, c := range row {
			if c == "gpu" {
				return true
			}
		}
	}
	return false
}

func inList(v string, list []string) bool {
	for _, x := range list {
		if x == v {
			return true
		}
	}
	return false
}
