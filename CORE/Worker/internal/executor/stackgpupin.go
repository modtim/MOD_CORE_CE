package executor

// megaplan §14.7/§14.7b — persistent stacks get the same per-card pinning
// node tasks already get (§14.2/§14.3): when the deploy task carries a
// GPUAssignment, rewrite the compose spec so each GPU-declaring service is
// pinned to the assigned card(s) instead of grabbing every GPU on the host.
//
// As of §14.7b the API plans each GPU-declaring compose service separately
// (routers/worker_tasks.py _assign_stack_service_gpu_cards, one kind/size per
// service instead of one for the whole stack) and sends the split as
// GPUAssignment.ServiceCards: {service_name: [card_id, ...]}. A stack mixing
// kinds (one cuda service + one rocm service) now gets BOTH pinned correctly,
// and several same-kind services can land on (and share) one card. A service
// absent from ServiceCards — or an older API that never set it — falls back
// to the task-wide Cards/Mode, i.e. today's mount-everything-pinned
// behaviour (see serviceGPUAssignment).

import (
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
	"gopkg.in/yaml.v3"
)

// pinStackGPUs rewrites `spec` so every service that declares an NVIDIA GPU
// reservation or mounts /dev/dri or /dev/kfd is pinned to the cards in `a`.
// A nil assignment, or one with no cards of a service's kind, leaves that
// service untouched — today's behaviour. Returns the rewritten spec and the
// names of services it changed (nil, spec unchanged, on any parse problem or
// when there is nothing to pin).
func pinStackGPUs(spec string, a *transport.GPUAssignment, resolve driResolver) (string, []string) {
	if a == nil || len(a.Cards) == 0 {
		return spec, nil
	}
	var doc yaml.Node
	if err := yaml.Unmarshal([]byte(spec), &doc); err != nil || len(doc.Content) == 0 {
		return spec, nil
	}
	services := mapValue(doc.Content[0], "services")
	if services == nil || services.Kind != yaml.MappingNode {
		return spec, nil
	}
	var changed []string
	for i := 0; i+1 < len(services.Content); i += 2 {
		name, svc := services.Content[i].Value, services.Content[i+1]
		if svc.Kind != yaml.MappingNode {
			continue
		}
		svcAssignment := serviceGPUAssignment(a, name)
		nvidiaIDs := assignedDeviceIDs(svcAssignment)
		rocmIdx := assignedIndicesForKind(svcAssignment, "rocm")
		intelIdx := assignedIndicesForKind(svcAssignment, "intel")

		did := false
		if len(nvidiaIDs) > 0 && pinNvidiaService(svc, nvidiaIDs) {
			did = true
		}
		if len(rocmIdx) > 0 && pinAmdIntelService(svc, "rocm", rocmIdx, svcAssignment, resolve) {
			did = true
		}
		if len(intelIdx) > 0 && pinAmdIntelService(svc, "intel", intelIdx, svcAssignment, resolve) {
			did = true
		}
		if did {
			changed = append(changed, name)
		}
	}
	if len(changed) == 0 {
		return spec, nil
	}
	out, err := yaml.Marshal(&doc)
	if err != nil {
		return spec, nil
	}
	sort.Strings(changed)
	return string(out), changed
}

// serviceGPUAssignment (§14.7b) narrows a task-wide GPUAssignment to just the
// cards `serviceName` was pinned to, when the API sent a per-service split
// (ServiceCards). A service absent from ServiceCards — including every
// service when the map is nil/empty (an older API, or a task with no split)
// — gets the whole task-wide `a` unchanged: today's behaviour, every
// GPU-declaring service is pinned to the full Cards/Mode.
func serviceGPUAssignment(a *transport.GPUAssignment, serviceName string) *transport.GPUAssignment {
	if len(a.ServiceCards) == 0 {
		return a
	}
	ids, ok := a.ServiceCards[serviceName]
	if !ok {
		return a
	}
	want := make(map[string]bool, len(ids))
	for _, id := range ids {
		want[id] = true
	}
	var cards []transport.AssignedGPU
	for _, c := range a.Cards {
		if want[c.CardID] {
			cards = append(cards, c)
		}
	}
	return &transport.GPUAssignment{
		Mode:        a.Mode,
		Cards:       cards,
		VRAMLimitMB: a.VRAMLimitMB,
		Source:      a.Source,
	}
}

// pinNvidiaService narrows a `deploy.resources.reservations.devices` NVIDIA
// entry (driver: nvidia, capabilities: [gpu]) from `count: all` to the
// assigned uuids, mirroring nvidiaDeviceRequest for a single-container task.
// Services with no such reservation block are untouched.
func pinNvidiaService(svc *yaml.Node, ids []string) bool {
	deploy := mapValue(svc, "deploy")
	if deploy == nil || deploy.Kind != yaml.MappingNode {
		return false
	}
	resources := mapValue(deploy, "resources")
	if resources == nil || resources.Kind != yaml.MappingNode {
		return false
	}
	reservations := mapValue(resources, "reservations")
	if reservations == nil || reservations.Kind != yaml.MappingNode {
		return false
	}
	devices := mapValue(reservations, "devices")
	if devices == nil || devices.Kind != yaml.SequenceNode {
		return false
	}
	did := false
	for _, dev := range devices.Content {
		if dev.Kind != yaml.MappingNode || !isNvidiaGPUReservation(dev) {
			continue
		}
		setDeviceIDs(dev, ids)
		did = true
	}
	return did
}

func isNvidiaGPUReservation(dev *yaml.Node) bool {
	if driver := mapValue(dev, "driver"); driver != nil && driver.Value != "" && driver.Value != "nvidia" {
		return false
	}
	caps := mapValue(dev, "capabilities")
	if caps == nil || caps.Kind != yaml.SequenceNode {
		return false
	}
	for _, c := range caps.Content {
		if strings.EqualFold(c.Value, "gpu") {
			return true
		}
	}
	return false
}

// setDeviceIDs replaces a device reservation's `count` with an explicit
// `device_ids` list, in place.
func setDeviceIDs(dev *yaml.Node, ids []string) {
	var kept []*yaml.Node
	for i := 0; i+1 < len(dev.Content); i += 2 {
		if dev.Content[i].Value == "count" || dev.Content[i].Value == "device_ids" {
			continue
		}
		kept = append(kept, dev.Content[i], dev.Content[i+1])
	}
	idSeq := &yaml.Node{Kind: yaml.SequenceNode, Tag: "!!seq"}
	for _, id := range ids {
		idSeq.Content = append(idSeq.Content, &yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: id})
	}
	kept = append(kept,
		&yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: "device_ids"}, idSeq)
	dev.Content = kept
}

// driResolver maps a pinned host index (+ preferred PCI bus id, exact when
// known) to that card's /dev/dri card and render paths. Swappable in tests;
// production uses driPathsFromSysfs.
type driResolver func(kind string, index int, preferBDF string) (cardPath, renderPath string)

// pinAmdIntelService narrows a service's `devices:` list to the assigned
// rocm/intel cards' /dev/dri nodes (keeping /dev/kfd, shared across all AMD
// cards) and sets the matching visible-devices env, reusing
// amdIntelVisibleDevicesEnv. Services with no /dev/dri or /dev/kfd device
// entry are untouched.
func pinAmdIntelService(svc *yaml.Node, kind string, indices []int, a *transport.GPUAssignment, resolve driResolver) bool {
	devices := mapValue(svc, "devices")
	if devices == nil || devices.Kind != yaml.SequenceNode || !mapsDRIOrKFD(devices) {
		return false
	}
	var kept []*yaml.Node
	dedup := map[string]bool{}
	for _, d := range devices.Content {
		src := deviceSource(d)
		if src == "/dev/kfd" {
			kept = append(kept, d)
			continue
		}
		if src == "/dev/dri" || strings.HasPrefix(src, "/dev/dri/") {
			continue // dropped — replaced below by the exact pinned node(s)
		}
		kept = append(kept, d)
	}
	mounted := 0
	for _, idx := range indices {
		busID := assignedPCIBusIDForIndex(a, kind, idx)
		cardPath, renderPath := resolve(kind, idx, busID)
		for _, p := range []string{cardPath, renderPath} {
			if p == "" || dedup[p] {
				continue
			}
			dedup[p] = true
			kept = append(kept, &yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: p})
			mounted++
		}
	}
	if mounted == 0 {
		return false
	}
	devices.Content = kept
	setVisibleDevicesEnv(svc, kind, len(indices))
	return true
}

// setVisibleDevicesEnv is the stack-service counterpart of
// amdIntelVisibleDevicesEnv, and it is NOT the same contract: the compose
// author's visible-devices value was written against the HOST's card
// numbering, but pinAmdIntelService just narrowed this service to `count`
// /dev/dri nodes, which ROCm/Level-Zero renumber to in-container 0..count-1.
// A serving profile rendered by the API (llm_serving_render.py) pins replica
// i to HIP_VISIBLE_DEVICES=i, so on a 2-card host replica 1 would ask for
// device 1 inside a container that only has one card mounted — it sees no GPU
// at all. So: honour an existing value only when it is already valid for the
// narrowed set (a comma list of distinct ints all < count); otherwise rewrite
// it to 0..count-1. A value we cannot interpret (a PCI bus id, a name) is
// left alone — rewriting it would be guessing.
func setVisibleDevicesEnv(svc *yaml.Node, kind string, count int) {
	if count <= 0 {
		return
	}
	var keys []string
	switch kind {
	case "rocm":
		keys = []string{"HIP_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES"}
	case "intel":
		keys = []string{"ZE_AFFINITY_MASK"}
	default:
		return
	}
	wanted := visibleDeviceRange(count)
	env := mapValue(svc, "environment")
	if env == nil {
		env = &yaml.Node{Kind: yaml.SequenceNode, Tag: "!!seq"}
		svc.Content = append(svc.Content,
			&yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: "environment"}, env)
	}
	// A service that pins neither key gets both (matching amdIntelVisibleDevicesEnv);
	// one that pins HIP but not ROCR keeps it that way — we never invent a second
	// key the author chose to omit.
	hipKey := keys[0]
	_, hipVal := mapEntry(env, hipKey)
	hipAbsent := hipVal == nil
	// Map form: rewrite in place, or append the keys that are absent.
	if env.Kind == yaml.MappingNode {
		for _, key := range keys {
			_, cur := mapEntry(env, key)
			if cur != nil {
				if visibleRangeValid(cur.Value, count) {
					continue
				}
				cur.Value = wanted
				continue
			}
			if key != hipKey && !hipAbsent {
				continue
			}
			env.Content = append(env.Content,
				&yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: key},
				&yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: wanted})
		}
		return
	}
	// List form ("K=V"): rewrite in place, append what is missing.
	hipAbsent = true
	for _, e := range env.Content {
		if strings.HasPrefix(e.Value, hipKey+"=") {
			hipAbsent = false
			break
		}
	}
	for _, key := range keys {
		target := key + "="
		var item *yaml.Node
		for _, e := range env.Content {
			if strings.HasPrefix(e.Value, target) {
				item = e
				break
			}
		}
		if item != nil {
			if visibleRangeValid(item.Value[strings.IndexByte(item.Value, '=')+1:], count) {
				continue
			}
			item.Value = target + wanted
			continue
		}
		if key != hipKey && !hipAbsent {
			continue
		}
		env.Content = append(env.Content,
			&yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: target + wanted})
	}
}

// mapEntry returns the value node of `key` in a mapping node (and the key node),
// or (nil, nil) when absent.
func mapEntry(m *yaml.Node, key string) (*yaml.Node, *yaml.Node) {
	if m == nil || m.Kind != yaml.MappingNode {
		return nil, nil
	}
	for i := 0; i+1 < len(m.Content); i += 2 {
		if m.Content[i].Value == key {
			return m.Content[i], m.Content[i+1]
		}
	}
	return nil, nil
}

// visibleDeviceRange is the in-container device list for `count` narrowed cards.
func visibleDeviceRange(count int) string {
	ids := make([]string, count)
	for i := range ids {
		ids[i] = strconv.Itoa(i)
	}
	return strings.Join(ids, ",")
}

// visibleRangeValid reports whether `value` is already a valid visible-devices
// value for a container holding exactly `count` cards: a comma list of distinct
// non-negative ints all < count. Anything else (a PCI bus id, a name, an out-of-
// range index such as the API's host-index pin) is not trusted.
func visibleRangeValid(value string, count int) bool {
	seen := map[string]bool{}
	for _, part := range strings.Split(value, ",") {
		part = strings.TrimSpace(part)
		n, err := strconv.Atoi(part)
		if err != nil || n < 0 || n >= count || seen[part] {
			return false
		}
		seen[part] = true
	}
	return len(seen) > 0
}

func mapsDRIOrKFD(devices *yaml.Node) bool {
	for _, d := range devices.Content {
		src := deviceSource(d)
		if src == "/dev/kfd" || src == "/dev/dri" || strings.HasPrefix(src, "/dev/dri/") {
			return true
		}
	}
	return false
}

// driPathsFromSysfs is the production driResolver: it mirrors executor.go's
// narrowDRIForKind (§14.3) so a stack service is pinned to the exact host
// card a node task would get for the same GPUAssignment.
func driPathsFromSysfs(kind string, index int, preferBDF string) (string, string) {
	validDrivers := map[string]bool{}
	switch kind {
	case "rocm":
		validDrivers["amdgpu"] = true
	case "intel":
		validDrivers["i915"] = true
		validDrivers["xe"] = true
	default:
		return "", ""
	}
	entries, err := os.ReadDir("/sys/class/drm")
	if err != nil {
		return "", ""
	}
	type drmDev struct{ bdf, cardPath, renderPath string }
	byBDF := map[string]*drmDev{}
	for _, entry := range entries {
		name := entry.Name()
		if !strings.HasPrefix(name, "card") {
			continue
		}
		if _, convErr := strconv.Atoi(strings.TrimPrefix(name, "card")); convErr != nil {
			continue
		}
		driverLink, dErr := os.Readlink(filepath.Join("/sys/class/drm", name, "device/driver"))
		if dErr != nil || !validDrivers[filepath.Base(driverLink)] {
			continue
		}
		devLink, lErr := os.Readlink(filepath.Join("/sys/class/drm", name, "device"))
		if lErr != nil {
			continue
		}
		bdf := filepath.Base(devLink)
		cardPath := "/dev/dri/" + name
		if _, sErr := os.Stat(cardPath); sErr != nil {
			continue
		}
		if _, ok := byBDF[bdf]; !ok {
			byBDF[bdf] = &drmDev{bdf: bdf, cardPath: cardPath}
		}
	}
	for _, entry := range entries {
		name := entry.Name()
		if !strings.HasPrefix(name, "renderD") {
			continue
		}
		devLink, lErr := os.Readlink(filepath.Join("/sys/class/drm", name, "device"))
		if lErr != nil {
			continue
		}
		if d, ok := byBDF[filepath.Base(devLink)]; ok {
			d.renderPath = "/dev/dri/" + name
		}
	}
	if preferBDF != "" {
		if d, ok := byBDF[preferBDF]; ok {
			return d.cardPath, d.renderPath
		}
	}
	var sorted []*drmDev
	for _, d := range byBDF {
		sorted = append(sorted, d)
	}
	sort.Slice(sorted, func(i, j int) bool { return sorted[i].bdf < sorted[j].bdf })
	if index < 0 || index >= len(sorted) {
		return "", ""
	}
	return sorted[index].cardPath, sorted[index].renderPath
}

// serviceEnvMap reads a compose service's `environment:` (map or "K=V" list
// form) into a plain map, so amdIntelVisibleDevicesEnv can honour a value the
// stack author already set (never re-emitted — same contract as node tasks).
func serviceEnvMap(svc *yaml.Node) map[string]string {
	env := mapValue(svc, "environment")
	out := map[string]string{}
	if env == nil {
		return out
	}
	switch env.Kind {
	case yaml.MappingNode:
		for i := 0; i+1 < len(env.Content); i += 2 {
			out[env.Content[i].Value] = env.Content[i+1].Value
		}
	case yaml.SequenceNode:
		for _, e := range env.Content {
			if eq := strings.IndexByte(e.Value, '='); eq >= 0 {
				out[e.Value[:eq]] = e.Value[eq+1:]
			}
		}
	}
	return out
}

// appendServiceEnv adds "K=V" entries to a service's `environment:`,
// matching whichever form (map or list) is already there, or creating a list
// if the service declares none. Keys already present are left to the caller
// to have filtered out (serviceEnvMap + amdIntelVisibleDevicesEnv do this).
func appendServiceEnv(svc *yaml.Node, kvs []string) {
	env := mapValue(svc, "environment")
	if env == nil {
		env = &yaml.Node{Kind: yaml.SequenceNode, Tag: "!!seq"}
		svc.Content = append(svc.Content,
			&yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: "environment"}, env)
	}
	switch env.Kind {
	case yaml.MappingNode:
		for _, kv := range kvs {
			eq := strings.IndexByte(kv, '=')
			env.Content = append(env.Content,
				&yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: kv[:eq]},
				&yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: kv[eq+1:]})
		}
	default:
		for _, kv := range kvs {
			env.Content = append(env.Content, &yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: kv})
		}
	}
}
