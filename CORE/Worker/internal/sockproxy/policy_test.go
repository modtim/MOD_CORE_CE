package sockproxy

import (
	"fmt"
	"os"
	"path/filepath"
	"testing"
)

// TestDecideEndpoints covers the endpoint allowlist (no body needed):
// which method+path pairs pass, which are denied, and that versioned
// (/vX.YY) paths parse identically to bare paths.
func TestDecideEndpoints(t *testing.T) {
	p := DefaultPolicy()
	cases := []struct {
		name   string
		method string
		path   string
		want   bool
	}{
		{"ping", "GET", "/_ping", true},
		{"version", "GET", "/version", true},
		{"info", "GET", "/info", true},
		{"events", "GET", "/events", true},
		{"containers json", "GET", "/containers/json?all=1", true},
		{"container inspect", "GET", "/containers/abc123/json", true},
		{"container logs", "GET", "/containers/abc123/logs?stdout=1", true},
		{"images json", "GET", "/images/json", true},
		{"image inspect", "GET", "/images/library/busybox/json", true},
		{"networks", "GET", "/networks", true},
		{"network inspect", "GET", "/networks/modnet", true},
		{"volumes", "GET", "/volumes", true},
		{"volume inspect", "GET", "/volumes/modvol", true},
		{"versioned version", "GET", "/v1.44/version", true},
		{"versioned containers json", "GET", "/v1.41/containers/json", true},
		{"versioned inspect", "GET", "/v1.45/containers/abc/json", true},
		{"swarm denied", "GET", "/swarm", false},
		{"secrets denied", "GET", "/secrets", false},
		{"nodes denied", "GET", "/nodes", false},
		{"plugins denied", "GET", "/plugins", false},
		{"archive get denied", "GET", "/containers/abc/archive?path=/", false},
		{"archive put denied", "PUT", "/containers/abc/archive", false},
		{"container top denied", "GET", "/containers/abc/top", false},
		{"image prune denied", "POST", "/images/prune", false},
		{"system prune denied", "POST", "/system/prune", false},
		{"image delete denied", "DELETE", "/images/busybox", false},
		{"volume delete denied", "DELETE", "/volumes/modvol", false},
		{"container start", "POST", "/containers/abc/start", true},
		{"container stop", "POST", "/containers/abc/stop", true},
		{"container kill", "POST", "/containers/abc/kill", true},
		{"container wait", "POST", "/containers/abc/wait", true},
		{"container remove", "POST", "/containers/abc/remove", true},
		{"container attach", "POST", "/containers/abc/attach?stream=1", true},
		{"exec start", "POST", "/exec/xyz/start", true},
		{"images create pull", "POST", "/images/create?fromImage=busybox", true},
		{"build", "POST", "/build?t=img&pull=1", true},
		{"build host net denied", "POST", "/build?networkmode=host", false},
		{"build host net versioned denied", "POST", "/v1.44/build?networkmode=host", false},
		{"network create", "POST", "/networks/create", true},
		{"network connect", "POST", "/networks/modnet/connect", true},
		{"network disconnect", "POST", "/networks/modnet/disconnect", true},
		{"volume create", "POST", "/volumes/create", true},
		{"plugins create denied", "POST", "/plugins/create", false},
		{"plugins push denied", "POST", "/plugins/xyz/push", false},
		{"auth denied", "POST", "/auth", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, _ := p.Decide(tc.method, tc.path, nil)
			if got != tc.want {
				t.Errorf("Decide(%s %s) = %v, want %v", tc.method, tc.path, got, tc.want)
			}
		})
	}
}

// TestDecideDenyReasons spot-checks that denials carry a human-readable
// reason (the 403 body in enforce mode) and allows carry none.
func TestDecideDenyReasons(t *testing.T) {
	p := DefaultPolicy()
	if allow, reason := p.Decide("GET", "/swarm", nil); allow || reason == "" {
		t.Errorf("swarm: allow=%v reason=%q, want deny with reason", allow, reason)
	}
	if allow, reason := p.Decide("GET", "/version", nil); !allow || reason != "" {
		t.Errorf("version: allow=%v reason=%q, want allow with no reason", allow, reason)
	}
	if allow, reason := p.Decide("POST", "/build?networkmode=host", nil); allow || reason == "" {
		t.Errorf("build hostnet: allow=%v reason=%q, want deny with reason", allow, reason)
	}
}

// TestDecideCreateBody covers the POST /containers/create HostConfig
// inspection (privilege escalation, host path binds, namespace modes,
// capabilities, GPU passthrough).
func TestDecideCreateBody(t *testing.T) {
	p := DefaultPolicy()
	cases := []struct {
		name string
		body string
		want bool
	}{
		{"plain create", `{"Image":"busybox"}`, true},
		{"hostconfig nil", `{"Image":"busybox","HostConfig":null}`, true},
		{"data bind", `{"HostConfig":{"Binds":["/data:/data"]}}`, true},
		{"privileged denied", `{"HostConfig":{"Privileged":true}}`, false},
		{"docker sock bind denied", `{"HostConfig":{"Binds":["/var/run/docker.sock:/var/run/docker.sock"]}}`, false},
		{"run docker sock bind denied", `{"HostConfig":{"Binds":["/run/docker.sock:/run/docker.sock:ro"]}}`, false},
		{"root bind denied", `{"HostConfig":{"Binds":["/:/host"]}}`, false},
		{"etc bind denied", `{"HostConfig":{"Binds":["/etc/passwd:/etc/passwd:ro"]}}`, false},
		{"etc prefix denied", `{"HostConfig":{"Binds":["/etc:/etc:ro"]}}`, false},
		{"root home denied", `{"HostConfig":{"Binds":["/root:/root"]}}`, false},
		{"proc bind denied", `{"HostConfig":{"Binds":["/proc:/proc:ro"]}}`, false},
		{"sys bind denied", `{"HostConfig":{"Binds":["/sys:/sys:ro"]}}`, false},
		{"docker lib bind denied", `{"HostConfig":{"Binds":["/var/lib/docker:/var/lib/docker:ro"]}}`, false},
		{"containerd denied", `{"HostConfig":{"Binds":["/run/containerd:/x"]}}`, false},
		{"containerd sock denied", `{"HostConfig":{"Binds":["/run/containerd/containerd.sock:/x"]}}`, false},
		{"containerd slash denied", `{"HostConfig":{"Binds":["/run/containerd/:/x"]}}`, false},
		{"kubelet denied", `{"HostConfig":{"Binds":["/var/lib/kubelet:/x"]}}`, false},
		{"kubelet sock denied", `{"HostConfig":{"Binds":["/var/lib/kubelet/kubelet.sock:/x"]}}`, false},
		{"kubelet slash denied", `{"HostConfig":{"Binds":["/var/lib/kubelet/:/x"]}}`, false},
		{"boot denied", `{"HostConfig":{"Binds":["/boot:/x"]}}`, false},
		{"boot vmlinuz denied", `{"HostConfig":{"Binds":["/boot/vmlinuz:/vmlinuz"]}}`, false},
		{"boot slash denied", `{"HostConfig":{"Binds":["/boot/:/x"]}}`, false},
		{"bootstrap allowed", `{"HostConfig":{"Binds":["/bootstrap:/x"]}}`, true},
		{"homework allowed", `{"HostConfig":{"Binds":["/homework:/x"]}}`, true},
		{"home denied", `{"HostConfig":{"Binds":["/home:/x"]}}`, false},
		{"home user ssh denied", `{"HostConfig":{"Binds":["/home/tim/.ssh:/x"]}}`, false},
		{"home user ro denied", `{"HostConfig":{"Binds":["/home/tim:/x:ro"]}}`, false},
		{"containerdx allowed", `{"HostConfig":{"Binds":["/run/containerdx:/x"]}}`, true},
		{"var lib containerd denied", `{"HostConfig":{"Binds":["/var/lib/containerd:/x"]}}`, false},
		{"var lib containerd cache denied", `{"HostConfig":{"Binds":["/var/lib/containerd/cache:/x"]}}`, false},
		{"var lib containerd slash denied", `{"HostConfig":{"Binds":["/var/lib/containerd/:/x"]}}`, false},
		{"lib modules denied", `{"HostConfig":{"Binds":["/lib/modules:/x"]}}`, false},
		{"lib modules kernel denied", `{"HostConfig":{"Binds":["/lib/modules/6.8/kernel:/x"]}}`, false},
		{"lib modules slash denied", `{"HostConfig":{"Binds":["/lib/modules/:/x"]}}`, false},
		{"lib itself denied by ancestor", `{"HostConfig":{"Binds":["/lib:/x"]}}`, false},
		{"libmodulesx allowed", `{"HostConfig":{"Binds":["/libmodulesx:/x"]}}`, true},
		{"lib modulesx allowed", `{"HostConfig":{"Binds":["/lib/modulesx:/x"]}}`, true},
		{"var lib containerdx allowed", `{"HostConfig":{"Binds":["/var/lib/containerdx:/x"]}}`, true},
		{"var lib docker other allowed", `{"HostConfig":{"Binds":["/var/lib/docker-other:/x"]}}`, true},
		{"dev null bind denied", `{"HostConfig":{"Binds":["/dev/null:/dev/null"]}}`, false},
		{"dev bind denied", `{"HostConfig":{"Binds":["/dev:/dev"]}}`, false},
		{"host pid denied", `{"HostConfig":{"PidMode":"host"}}`, false},
		{"host ipc denied", `{"HostConfig":{"IpcMode":"host"}}`, false},
		{"host uts denied", `{"HostConfig":{"UTSMode":"host"}}`, false},
		{"host net denied", `{"HostConfig":{"NetworkMode":"host"}}`, false},
		{"host userns denied", `{"HostConfig":{"UsernsMode":"host"}}`, false},
		{"host cgroupns denied", `{"HostConfig":{"CgroupnsMode":"host"}}`, false},
		{"unconfined denied", `{"HostConfig":{"SecurityOpt":["label=unconfined"]}}`, false},
		{"noprofile ok", `{"HostConfig":{"SecurityOpt":["no-new-privileges"]}}`, true},
		{"capadd sysadmin denied", `{"HostConfig":{"CapAdd":["SYS_ADMIN"]}}`, false},
		{"capadd netbindservice ok", `{"HostConfig":{"CapAdd":["NET_BIND_SERVICE"]}}`, true},
		{"capadd sysnice ok", `{"HostConfig":{"CapAdd":["SYS_NICE","IPC_LOCK"]}}`, true},
		{"nvidia devreq allowed", `{"HostConfig":{"DeviceRequests":[{"Driver":"nvidia"}]}}`, true},
		{"gpu cap devreq allowed", `{"HostConfig":{"DeviceRequests":[{"Capabilities":[["gpu"]]}]}}`, true},
		{"other driver devreq denied", `{"HostConfig":{"DeviceRequests":[{"Driver":"fakedev"}]}}`, false},
		{"runtime nvidia allowed", `{"HostConfig":{"Runtime":"nvidia"}}`, true},
		{"runtime plain allowed", `{"HostConfig":{"Runtime":""}}`, true},
		{"runtime other denied", `{"HostConfig":{"Runtime":"myrt"}}`, false},
		{"kfd device allowed", `{"HostConfig":{"Devices":[{"PathOnHost":"/dev/kfd"}]}}`, true},
		{"dri device allowed", `{"HostConfig":{"Devices":[{"PathOnHost":"/dev/dri/card0"}]}}`, true},
		{"dxg device allowed", `{"HostConfig":{"Devices":[{"PathOnHost":"/dev/dxg"}]}}`, true},
		{"nvidia device allowed", `{"HostConfig":{"Devices":[{"PathOnHost":"/dev/nvidia0"}]}}`, true},
		{"nvidia10 device allowed", `{"HostConfig":{"Devices":[{"PathOnHost":"/dev/nvidia10"}]}}`, true},
		{"wsl ro bind allowed", `{"HostConfig":{"Binds":["/usr/lib/wsl:/usr/lib/wsl:ro"]}}`, true},
		{"mounts bind etc denied", `{"HostConfig":{"Mounts":[{"Type":"bind","Source":"/etc","Target":"/etc"}]}}`, false},
		{"mounts bind sock denied", `{"HostConfig":{"Mounts":[{"Type":"bind","Source":"/var/run/docker.sock","Target":"/var/run/docker.sock"}]}}`, false},
		{"mounts volume ok", `{"HostConfig":{"Mounts":[{"Type":"volume","Source":"myvol","Target":"/x"}]}}`, true},
		{"mounts tmpfs ok", `{"HostConfig":{"Mounts":[{"Type":"tmpfs","Source":"tmpfs","Target":"/tmp/x"}]}}`, true},
		{"mounts volume named allowed", `{"HostConfig":{"Mounts":[{"Type":"volume","Source":"v1","Target":"/x"}]}}`, true},
		{"mounts volume anonymous allowed", `{"HostConfig":{"Mounts":[{"Type":"volume","Target":"/x"}]}}`, true},
		{"mounts tmpfs target only allowed", `{"HostConfig":{"Mounts":[{"Type":"tmpfs","Target":"/tmp/x"}]}}`, true},
		{"mounts volumeoptions device data allowed", `{"HostConfig":{"Mounts":[{"Type":"volume","Target":"/x","VolumeOptions":{"DriverConfig":{"Driver":"local","Options":{"device":"/data","o":"bind"}}}}]}}`, true},
		{"mounts volumeoptions local device root denied", `{"HostConfig":{"Mounts":[{"Type":"volume","Target":"/host","VolumeOptions":{"BindEnabled":true,"DriverConfig":{"Driver":"local","Options":{"device":"/","o":"bind"}}}}]}}`, false},
		{"mounts volumeoptions device etc denied", `{"HostConfig":{"Mounts":[{"Type":"volume","Target":"/x","VolumeOptions":{"DriverConfig":{"Driver":"local","Options":{"device":"/etc"}}}}]}}`, false},
		{"mounts volumeoptions device docker lib denied", `{"HostConfig":{"Mounts":[{"Type":"volume","Target":"/x","VolumeOptions":{"DriverConfig":{"Driver":"local","Options":{"device":"/var/lib/docker/x"}}}}]}}`, false},
		{"mounts volumeoptions bindenabled no options denied", `{"HostConfig":{"Mounts":[{"Type":"volume","Target":"/x","VolumeOptions":{"BindEnabled":true}}]}}`, false},
		{"mounts volumeoptions driver foo denied", `{"HostConfig":{"Mounts":[{"Type":"volume","Target":"/x","VolumeOptions":{"DriverConfig":{"Driver":"foo"}}}]}}`, false},
		{"mounts volumeoptions tmpfs with opts denied", `{"HostConfig":{"Mounts":[{"Type":"volume","Target":"/x","VolumeOptions":{"DriverConfig":{"Driver":"tmpfs","Options":{"device":"/data"}}}}]}}`, false},
		{"mounts volumeoptions o bind no device denied", `{"HostConfig":{"Mounts":[{"Type":"volume","Target":"/x","VolumeOptions":{"DriverConfig":{"Driver":"local","Options":{"o":"bind"}}}}]}}`, false},
		{"binds named volume denied", `{"HostConfig":{"Binds":["v1:/x"]}}`, false},
		{"binds named volume ro denied", `{"HostConfig":{"Binds":["v1:/x:ro"]}}`, false},
		{"mounts bind data allowed", `{"HostConfig":{"Mounts":[{"Type":"bind","Source":"/data","Target":"/x"}]}}`, true},
		{"bad json denied", `{not json`, false},
		{"nil body denied", "", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, reason := p.Decide("POST", "/containers/create", []byte(tc.body))
			if got != tc.want {
				t.Errorf("Decide(create %s) = %v (reason %q), want %v", tc.body, got, reason, tc.want)
			}
		})
	}
}

// TestDecideCreateVersioned checks the same body rule applies through a
// version-prefixed path.
func TestDecideCreateVersioned(t *testing.T) {
	p := DefaultPolicy()
	if allow, _ := p.Decide("POST", "/v1.44/containers/create", []byte(`{"HostConfig":{"Privileged":true}}`)); allow {
		t.Error("versioned privileged create must be denied")
	}
}

// TestDecideExecCreate covers the exec-create Privileged check.
func TestDecideExecCreate(t *testing.T) {
	p := DefaultPolicy()
	if allow, _ := p.Decide("POST", "/containers/abc/exec", []byte(`{"Cmd":["ls"],"Privileged":true}`)); allow {
		t.Error("privileged exec create must be denied")
	}
	if allow, reason := p.Decide("POST", "/containers/abc/exec", []byte(`{"Cmd":["ls"]}`)); !allow {
		t.Errorf("plain exec create denied: %q", reason)
	}
}

// TestHostNetworkAllowlistFlag checks the stack-mode override.
func TestHostNetworkAllowlistFlag(t *testing.T) {
	p := DefaultPolicy()
	p.AllowHostNetwork = true
	if allow, _ := p.Decide("POST", "/containers/create", []byte(`{"HostConfig":{"NetworkMode":"host"}}`)); !allow {
		t.Error("NetworkMode host must be allowed when AllowHostNetwork is set")
	}
}

// TestValidateSourceNormalization covers S-1: the deny-list must see a
// normalized path, so no formatted form of a reserved host path passes
// (traversal, double slash, trailing slash, dot components, the /var/run
// -> /run symlink equivalent), and non-normalizable sources (relative,
// empty, NUL byte) are rejected fail-closed. Legitimate sources (the
// ones the body tests already use) stay allowed UNCHANGED, and the
// component-aware prefix test keeps /etcfoo, /varfoo/..., /procfoo out
// of the /etc, /var/lib/docker and /proc denies.
func TestValidateSourceNormalization(t *testing.T) {
	p := DefaultPolicy()
	cases := []struct {
		name string
		src  string
		want bool
	}{
		// traversal / formatting forms of denied paths: all denied
		{"trav usr etc", "/usr/../etc", false},
		{"trav usr etc passwd", "/usr/../etc/passwd", false},
		{"trav varrun docker sock", "/var/run/../docker.sock", false},
		{"double slash varrun", "/var/run//docker.sock", false},
		{"trailing slash varrun", "/var/run/docker.sock/", false},
		{"dot component varrun", "/var/run/./docker.sock", false},
		{"run docker sock symlink equiv", "/run/docker.sock", false},
		{"trav docker sock up", "/var/run/docker.sock/../docker.sock", false},
		{"trailing dot docker sock", "/var/run/docker.sock/.", false},
		{"trav usr to root", "/usr/..", false},
		{"trav varlibdocker", "/var/lib/docker/../docker", false},
		{"trav etc self", "/etc/../etc/passwd", false},
		{"trav dev to etc", "/dev/../etc", false},
		{"trav dri to etc", "/dev/dri/../../etc", false},
		{"trav mixed dots", "/usr/.././../etc", false},
		{"plain varrun docker sock", "/var/run/docker.sock", false},
		{"relative dot slash", "./relative", false},
		{"empty source", "", false},
		{"nul byte", "/etc\x00passwd", false},
		{"bare dot", ".", false},
		{"bare dotdot", "..", false},
		// legitimate sources, unchanged from the body tests
		{"data bind", "/data", true},
		{"data sub", "/data/x/y", true},
		{"wsl lib bind", "/usr/lib/wsl", true},
		{"wsl lib sub", "/usr/lib/wsl/lib", true},
		{"var runner not var run", "/var/runner", true},
		{"usr local data", "/usr/local/data", true},
		{"etc foo not etc", "/etcfoo", true},
		{"proc foo not proc", "/procfoo", true},
		{"varfoo lib docker not varlibdocker", "/varfoo/lib/docker", true},
		{"var run dir itself now denied", "/var/run", false},
		{"var run no slash denied", "/var/run", false},
		{"var run trailing slash denied", "/var/run/", false},
		{"run dir itself denied", "/run", false},
		{"var dir itself denied", "/var", false},
		{"var lib denied", "/var/lib", false},
		{"host root denied", "/", false},
		{"var run up denied", "/var/run/..", false},
		{"var run up up denied", "/var/run/../..", false},
		{"sock suffix not denied", "/var/run/docker.sockx", true},
		{"nvidia dev", "/dev/nvidia0", true},
		{"nvidia10 dev", "/dev/nvidia10", true},
		{"nvidia entry", "/dev/nvidia", true},
		{"dri card", "/dev/dri/card0", true},
		{"kfd dev", "/dev/kfd", true},
		{"dxg dev", "/dev/dxg", true},
		{"dotdot even when it cancels", "/usr/local/../local/data", false},
		{"varfoo not var", "/varfoo", true},
		{"var run docker dir not sock", "/var/run/docker", true},
		{"var-data not var", "/var-data", true},
		{"double slash data", "/data//x", true},
		{"dot slash data", "/data/./x", true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, reason := validateSource(tc.src, p)
			if got != tc.want {
				t.Errorf("validateSource(%q) = %v (reason %q), want %v", tc.src, got, reason, tc.want)
			}
		})
	}
	// the path used downstream is the cleaned one, not the raw input
	if normPath("/usr/local/./data//x/.") != "/usr/local/data/x" {
		t.Errorf("normPath(/usr/local/./data//x/.) = %q, want /usr/local/data/x", normPath("/usr/local/./data//x/."))
	}
	if normPath("/var/run//docker.sock") != "/var/run/docker.sock" {
		t.Errorf("normPath double slash not collapsed: %q", normPath("/var/run//docker.sock"))
	}
	if normPath("/usr/../etc") != "" || normPath("./relative") != "" || normPath("/etc\x00passwd") != "" {
		t.Error("normPath must return \"\" for traversal, relative and NUL sources")
	}
}

// TestCapAllowlistOverride checks a configurable capability list.
func TestCapAllowlistOverride(t *testing.T) {
	p := DefaultPolicy()
	p.CapAllowlist = []string{"SYS_ADMIN"}
	if allow, _ := p.Decide("POST", "/containers/create", []byte(`{"HostConfig":{"CapAdd":["SYS_ADMIN"]}}`)); !allow {
		t.Error("SYS_ADMIN must be allowed when overridden into the list")
	}
	if allow, _ := p.Decide("POST", "/containers/create", []byte(`{"HostConfig":{"CapAdd":["NET_ADMIN"]}}`)); allow {
		t.Error("NET_ADMIN must be denied when it is not in the overridden list")
	}
}

// TestSockproxyAncestorSymlinkConsistency covers the follow-up to the
// normalization fix: (a) an ANCESTOR of a denied path is denied (a bind of
// /var/run mounts /var/run/docker.sock at mount time, exactly like binding
// the socket), (c) the RESOLVED path is checked too - a symlink planted
// under an allowed dir must not reach a denied target, and a symlink loop
// fails closed - and (d) the create body refuses a source that is not
// already canonical, so the value validated is the value forwarded.
// The deny list is a package variable, so the test swaps it for a
// temporary list pointing at real temp dirs (no host path is involved) and
// restores it on exit.
func TestSockproxyAncestorSymlinkConsistency(t *testing.T) {
	dir := t.TempDir()
	denied := filepath.Join(dir, "denied-target")
	if err := os.Mkdir(denied, 0o755); err != nil {
		t.Errorf("mkdir %s: %v", denied, err)
		return
	}
	allowed := filepath.Join(dir, "allowed-target")
	if err := os.Mkdir(allowed, 0o755); err != nil {
		t.Errorf("mkdir %s: %v", allowed, err)
		return
	}
	saved := deniedHostPrefixes
	deniedHostPrefixes = []string{denied, allowed + "/nested"}
	defer func() { deniedHostPrefixes = saved }()
	p := DefaultPolicy()
	// ancestor of the temp denied path is denied (the rule under test)
	if ok, _ := validateSource(dir, p); ok {
		t.Errorf("ancestor of a denied path (%q) must be denied", dir)
	}
	// symlink to a denied target is rejected
	link := filepath.Join(dir, "link-to-denied")
	if err := os.Symlink(denied, link); err != nil {
		t.Errorf("symlink %s: %v", link, err)
		return
	}
	if ok, reason := validateSource(link, p); ok {
		t.Errorf("symlink to denied target accepted: %q", reason)
	}
	// symlink to a target that is neither denied nor an ancestor of a
	// denied path is accepted
	okTarget := filepath.Join(dir, "ok-target")
	if err := os.Mkdir(okTarget, 0o755); err != nil {
		t.Errorf("mkdir %s: %v", okTarget, err)
		return
	}
	linkOk := filepath.Join(dir, "link-to-allowed")
	if err := os.Symlink(okTarget, linkOk); err != nil {
		t.Errorf("symlink %s: %v", linkOk, err)
		return
	}
	if ok, reason := validateSource(linkOk, p); !ok {
		t.Errorf("symlink to allowed target rejected: %q", reason)
	}
	// the nested target must exist so EvalSymlinks resolves instead of
	// reporting a missing path
	if err := os.MkdirAll(filepath.Join(allowed, "nested", "deep"), 0o755); err != nil {
		t.Errorf("mkdir nested: %v", err)
		return
	}
	// symlink resolving under a DENIED ancestor (allowed/nested is denied)
	linkNested := filepath.Join(dir, "link-to-nested")
	if err := os.Symlink(allowed + "/nested/deep", linkNested); err != nil {
		t.Errorf("symlink %s: %v", linkNested, err)
		return
	}
	if ok, _ := validateSource(linkNested, p); ok {
		t.Error("symlink resolving under a denied ancestor must be rejected")
	}
	// symlink loop is rejected (fail closed, not treated as missing)
	loopA := filepath.Join(dir, "loop-a")
	loopB := filepath.Join(dir, "loop-b")
	if err := os.Symlink(loopB, loopA); err != nil {
		t.Errorf("symlink %s: %v", loopA, err)
		return
	}
	if err := os.Symlink(loopA, loopB); err != nil {
		t.Errorf("symlink %s: %v", loopB, err)
		return
	}
	if ok, reason := validateSource(loopA, p); ok {
		t.Errorf("symlink loop accepted: %q", reason)
	}
	// a source that does not exist yet keeps the cleaned-only check
	missing := filepath.Join(okTarget, "not-created-yet")
	if ok, _ := validateSource(missing, p); !ok {
		t.Error("a non-existing source under an allowed dir must stay allowed")
	}
	// (e) the device digit rule is ASCII digits only
	if p.deviceAllowed("/dev/nvidia\u0660") {
		t.Error("Arabic-Indic digit 0 must not match /dev/nvidia")
	}
	if p.deviceAllowed("/dev/nvidia\uff10") {
		t.Error("fullwidth digit 0 must not match /dev/nvidia")
	}
	if p.deviceAllowed("/dev/nvidia0etc") {
		t.Error("non-digit suffix /dev/nvidia0etc must not match /dev/nvidia")
	}
	if !p.deviceAllowed("/dev/nvidia42") {
		t.Error("ASCII digit suffix /dev/nvidia42 must match /dev/nvidia")
	}
	// (d) the create body refuses a non-canonical source (the value validated
	// is the value forwarded)
	if allow, _ := p.Decide("POST", "/containers/create", []byte(`{"HostConfig":{"Binds":["/data//x:/data/x"]}}`)); allow {
		t.Error("create with non-canonical bind /data//x must be denied")
	}
	if allow, _ := p.Decide("POST", "/containers/create", []byte(`{"HostConfig":{"Binds":["/data/./x:/data/x"]}}`)); allow {
		t.Error("create with non-canonical bind /data/./x must be denied")
	}
	if allow, _ := p.Decide("POST", "/containers/create", []byte(`{"HostConfig":{"Mounts":[{"Type":"bind","Source":"/data/./x","Target":"/x"}]}}`)); allow {
		t.Error("create with non-canonical mount source /data/./x must be denied")
	}
	if allow, _ := p.Decide("POST", "/containers/create", []byte(`{"HostConfig":{"Binds":["/data/x:/data/x"]}}`)); !allow {
		t.Error("canonical legit bind /data/x must be allowed")
	}
}

// TestSockproxyMissingTailSymlinkBypass: a bind source whose last
// components do not exist yet, under a symlink to a denied directory (or
// a dangling symlink to a denied path), must be denied: dockerd creates a
// missing Binds source as a host directory.
func TestSockproxyMissingTailSymlinkBypass(t *testing.T) {
	dir := t.TempDir()
	denied := filepath.Join(dir, "denied-target")
	allowed := filepath.Join(dir, "allowed-target")
	farm := filepath.Join(dir, "farm")
	for _, d := range []string{denied, allowed, farm} {
		if err := os.Mkdir(d, 0o755); err != nil {
			t.Fatalf("mkdir %s: %v", d, err)
		}
	}
	saved := deniedHostPrefixes
	deniedHostPrefixes = []string{denied}
	defer func() { deniedHostPrefixes = saved }()
	p := DefaultPolicy()
	link := func(target, name string) string {
		l := filepath.Join(farm, name)
		if err := os.Symlink(target, l); err != nil {
			t.Fatalf("symlink %s: %v", l, err)
		}
		return l
	}
	e := link(denied, "e")
	d1 := link(filepath.Join(denied, "secret", "x"), "d")
	d2 := link(filepath.Join(denied, "nonexist"), "d2")
	rel := link("../denied-target/rel/missing", "rel")
	okLink := link(allowed, "ok")
	deny := map[string]string{
		"missing tail one level":   filepath.Join(e, "docker.service.d"),
		"missing tail two levels":  filepath.Join(e, "a", "b"),
		"dangling to denied/x":     d1,
		"dangling to denied/nonex": d2,
		"dangling below missing":   filepath.Join(d1, "more", "tail"),
		"relative dangling":        rel,
	}
	for name, src := range deny {
		if ok, reason := validateSource(src, p); ok {
			t.Errorf("%s: %q accepted (reason %q)", name, src, reason)
		}
	}
	// loop a -> b -> a is denied, also with a missing tail below it
	la, lb := filepath.Join(farm, "la"), filepath.Join(farm, "lb")
	if err := os.Symlink(lb, la); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(la, lb); err != nil {
		t.Fatal(err)
	}
	for _, src := range []string{la, filepath.Join(la, "tail")} {
		if ok, _ := validateSource(src, p); ok {
			t.Errorf("symlink loop source %q accepted", src)
		}
	}
	// a chain of 41 dangling links (ending at a missing allowed path) is
	// denied by the depth cap
	missingEnd := filepath.Join(allowed, "never")
	next := missingEnd
	for i := 40; i >= 0; i-- {
		next = link(next, fmt.Sprintf("c%02d", i))
	}
	if ok, _ := validateSource(filepath.Join(farm, "c00"), p); ok {
		t.Error("chain of 41 dangling links accepted (depth cap)")
	}
	// controls: still accepted
	accept := map[string]string{
		"symlink to allowed + missing tail":  filepath.Join(okLink, "m", "n"),
		"dangling symlink to allowed missing": link(filepath.Join(allowed, "later"), "dl"),
		"plain missing under allowed root":   filepath.Join(allowed, "x", "y", "z"),
		"short dangling chain to allowed":    link(filepath.Join(farm, "dl"), "dl2"),
	}
	for name, src := range accept {
		if ok, reason := validateSource(src, p); !ok {
			t.Errorf("control %s: %q rejected: %s", name, src, reason)
		}
	}
}

// TestDecideVolumesCreateBody covers the POST /volumes/create body rule
// (DIAG C3 / proposal P1): a local volume whose DriverOpts.device names a
// host path is a bind mount in disguise. The test mirrors what the proxy
// does (proxy_handle.go maybeBuffer): the body is only handed to Decide
// when wantsBody says the endpoint is inspected, so a wantsBody that
// stopped reporting /volumes/create fails the whole device-deny group.
// Empty-body convention: an EMPTY (zero-length, non-nil) body is denied
// as "not valid JSON"; a nil body (no body at all, only reachable from a
// direct Decide caller since wantsBody is true here) is allowed, matching
// the endpoint-allowlist convention that the endpoint itself is allowed.
func TestDecideVolumesCreateBody(t *testing.T) {
	p := DefaultPolicy()
	cases := []struct {
		name string
		body string
		want bool
	}{
		{"plain volume", `{"Name":"v"}`, true},
		{"driver local no opts", `{"Name":"v","Driver":"local"}`, true},
		{"empty driver", `{"Name":"v","Driver":""}`, true},
		{"tmpfs no opts", `{"Name":"v","Driver":"tmpfs"}`, true},
		{"local fstype only", `{"Name":"v","Driver":"local","DriverOpts":{"fstype":"none"}}`, true},
		{"device data allowed", `{"Name":"v","Driver":"local","DriverOpts":{"device":"/data"}}`, true},
		{"device data sub allowed", `{"Name":"v","Driver":"local","DriverOpts":{"device":"/data/task/x"}}`, true},
		{"device data o bind allowed", `{"Name":"v","Driver":"local","DriverOpts":{"device":"/data","o":"bind"}}`, true},
		{"device root denied", `{"Name":"v","Driver":"local","DriverOpts":{"device":"/","fstype":"none","o":"bind"}}`, false},
		{"device etc denied", `{"Name":"v","Driver":"local","DriverOpts":{"device":"/etc"}}`, false},
		{"device docker lib denied", `{"Name":"v","Driver":"local","DriverOpts":{"device":"/var/lib/docker/x"}}`, false},
		{"device sock denied", `{"Name":"v","Driver":"local","DriverOpts":{"device":"/var/run/docker.sock"}}`, false},
		{"device ancestor of sock denied", `{"Name":"v","Driver":"local","DriverOpts":{"device":"/var/run"}}`, false},
		{"device boot denied", `{"Name":"v","Driver":"local","DriverOpts":{"device":"/boot"}}`, false},
		{"device traversal denied", `{"Name":"v","Driver":"local","DriverOpts":{"device":"/data/../etc"}}`, false},
		{"device relative denied", `{"Name":"v","Driver":"local","DriverOpts":{"device":"data"}}`, false},
		{"device empty denied", `{"Name":"v","Driver":"local","DriverOpts":{"device":""}}`, false},
		{"o bind no device denied", `{"Name":"v","Driver":"local","DriverOpts":{"o":"bind"}}`, false},
		{"o bind comma denied", `{"Name":"v","Driver":"local","DriverOpts":{"o":"bind,ro"}}`, false},
		{"driver foo denied", `{"Name":"v","Driver":"foo"}`, false},
		{"driver plugin denied", `{"Name":"v","Driver":"localx"}`, false},
		{"tmpfs with opts denied", `{"Name":"v","Driver":"tmpfs","DriverOpts":{"device":"/data"}}`, false},
		{"bad json denied", `{not json`, false},
		{"empty body denied", ``, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			// mirror maybeBuffer: only inspected endpoints get a body
			buf := wantsBody("POST", "/volumes/create")
			body := []byte(tc.body)
			if !buf {
				body = nil
			}
			got, reason := p.Decide("POST", "/volumes/create", body)
			if got != tc.want {
				t.Errorf("Decide(volumes/create %s) = %v (reason %q), want %v", tc.body, got, reason, tc.want)
			}
			if !got && reason == "" {
				t.Errorf("denial for %s carries no reason", tc.name)
			}
		})
	}

	// denial reasons must not echo the whole body
	if _, reason := p.Decide("POST", "/volumes/create",
		[]byte(`{"Name":"v","Driver":"foo","DriverOpts":{"device":"/data/secret"}}`)); reason != "" && len(reason) > 80 {
		t.Errorf("volume create denial reason too long / echoes body: %q", reason)
	}

	// a DriverOpts.device that is a symlink into a denied directory must be
	// denied: the same resolved-path check validateSource gives a bind.
	// Same temp-dir + swapped deny list pattern as the symlink tests above.
	dir := t.TempDir()
	denied := filepath.Join(dir, "denied-target")
	if err := os.Mkdir(denied, 0o755); err != nil {
		t.Errorf("mkdir %s: %v", denied, err)
		return
	}
	saved := deniedHostPrefixes
	deniedHostPrefixes = []string{denied}
	defer func() { deniedHostPrefixes = saved }()
	link := filepath.Join(dir, "link-into-denied")
	if err := os.Symlink(filepath.Join(denied, "deep"), link); err != nil {
		t.Errorf("symlink %s: %v", link, err)
		return
	}
	if ok, reason := p.Decide("POST", "/volumes/create",
		[]byte(`{"Name":"v","Driver":"local","DriverOpts":{"device":"`+link+`"}}`)); ok {
		t.Errorf("volume device symlink into denied dir %q accepted: %q", link, reason)
	}
}
