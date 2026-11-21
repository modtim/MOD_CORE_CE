package executor

// B86i — containers of a MOD persistent stack could never reach the MOD API
// when the API is CO-LOCATED on the deploying host: the host's /etc/hosts maps
// the API hostname (e.g. example.com) to 127.0.0.1, and that entry
// leaks through docker DNS into every stack container, so the app dials ITSELF
// and every files/secrets call is ECONNREFUSED (stack 114, modstack114g0-api-1
// on modstack114g0_mod-stack-egress, ExtraHosts []).
//
// The platform fix (no per-app compose hacks): at the single stack-compose
// render point, build a candidate-host set — the worker's own apiURL host
// PLUS every hostname parsed out of an http(s):// URL living in a service's
// `environment` (an app rarely calls the worker's apiURL directly; it calls
// whatever platform URL it was configured with, e.g.
// CONSIGN_MOD_API_URL=https://example.com, which independently
// leaks to loopback via the same host /etc/hosts). For each candidate host
// that resolves to a loopback address on this host, inject
// `<host>:host-gateway` into every service's extra_hosts so docker re-maps it
// to the host's routable gateway IP. A user-authored extra_hosts entry for
// that hostname is never overridden. A service carrying the advisory
// MOD_ALLOWED_EGRESS_HOSTS env gets the host appended when missing, so an
// allowlist posture never blocks the platform call the app is making.
//
// B86i-f — a single NXDOMAIN right after the worker container's own network
// attach was being trusted as a definitive "not co-located" negative, so the
// B86i-e retry never got a chance to see the real (loopback) answer a few
// hundred ms later; see resolveAPIHostAddrs. Every silent no-op path also now
// logs at Debug (and genuine resolver failures still log at Warn), so
// "nothing needed injecting" is never indistinguishable, from `docker logs
// my_mod_worker`, from "injection silently failed".

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/netip"
	"os"
	"sort"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

// egressEnvVar is the advisory per-service egress allowlist env the API renders
// for `hosts`-posture stack services (routers/persistent_stacks
// _apply_egress_networks; same grammar as netpol.go's node-task env).
const egressEnvVar = "MOD_ALLOWED_EGRESS_HOSTS"

// apiHostFromURL extracts the bare hostname from a worker API URL
// (https://example.com/v1 -> example.com). Scheme-less
// config is common in the wild, so a missing scheme is accepted.
func apiHostFromURL(apiURL string) string {
	s := strings.TrimSpace(apiURL)
	if i := strings.Index(s, "://"); i >= 0 {
		s = s[i+3:]
	}
	for i := 0; i < len(s); i++ {
		if c := s[i]; c == '/' || c == '?' || c == '&' || c == ':' {
			s = s[:i]
			break
		}
	}
	return strings.TrimSpace(s)
}

// isLoopbackAddr reports whether a resolved address string is loopback — the
// leak signature B86i describes (127.x.x.x, ::1, or the unspecified form).
func isLoopbackAddr(s string) bool {
	a, err := netip.ParseAddr(s)
	if err != nil {
		return false
	}
	if a.IsUnspecified() {
		// 0.0.0.0 / :: — never a real route; treat as unusable, same as loopback.
		return true
	}
	return a.IsLoopback()
}

// hostOfExtraHost returns the hostname half of an extra_hosts entry
// ("site:1.2.3.4" -> "site"; "site:host-gateway" -> "site").
func hostOfExtraHost(entry string) string {
	h, _, _ := partitionOnce(strings.TrimSpace(entry), ":")
	return strings.TrimSpace(h)
}

// extraHostsHas reports whether a service already declares an extra_hosts entry
// for hostname (any "<host>:..." form) — the user's own mapping wins. Reads
// through a service-level YAML merge key (`<<: *anchor`) the same way
// collectEnvURLHosts does, so a pinned host hiding behind an anchor is still
// honored.
func extraHostsHas(svc *yaml.Node, host string) bool {
	eh := mapGet(resolveMergedMapping(svc), "extra_hosts")
	if eh == nil || eh.Kind != yaml.SequenceNode {
		// `extra_hosts:` with a null value declares nothing.
		return false
	}
	for _, item := range eh.Content {
		if hostOfExtraHost(item.Value) == host {
			return true
		}
	}
	return false
}

// allowlistAppend adds host to the service's MOD_ALLOWED_EGRESS_HOSTS env when
// that env exists and lacks the host, so an allowlist posture never blocks the
// platform call. Absence of the env means "no advisory list" -> no change.
//
// B86i-d — the value this env ever actually carries is COMMA-joined: the API
// renders it with `",".join(hosts)` (persistent_stacks/_shared.py's
// _apply_egress_networks), the same convention netpol.go uses for the
// node-task grammar this env mirrors (`strings.Join(payload.AllowedEgress,
// ",")`). Appending with a bare space here produced a value no consumer of
// either convention could parse back out (e.g. stack 114's api service:
// "...,upload.ebay.com,example.com" already comma-joined — a
// naive space-token scan never finds "example.com" in that
// string, so a later run always re-appends it, space-separated, onto an
// already-comma-separated list).
func allowlistAppend(svc *yaml.Node, host string) {
	env := mapGet(svc, "environment")
	if env == nil {
		return
	}
	if env.Kind == yaml.MappingNode {
		// `env: {MOD_ALLOWED_EGRESS_HOSTS: "a,b"}` — append the host token.
		v := mapGet(env, egressEnvVar)
		if v != nil && v.Kind == yaml.ScalarNode && !hasToken(v.Value, host) {
			v.Value = appendAllowlistToken(v.Value, host)
		}
		return
	}
	if env.Kind == yaml.SequenceNode {
		for _, item := range env.Content {
			k, found, val := partitionOnce(item.Value, "=")
			if !found || strings.TrimSpace(k) != egressEnvVar {
				continue
			}
			if !hasToken(val, host) {
				// item.Value is a copy — write the rebuilt string back onto
				// the node so compose re-emits it.
				item.Value = strings.TrimSpace(k) + "=" + appendAllowlistToken(val, host)
			}
		}
	}
}

// appendAllowlistToken adds host to an existing MOD_ALLOWED_EGRESS_HOSTS value
// using the separator the value already uses — comma when the value has one
// (the only shape the API ever renders), space otherwise (an empty or
// single-token value, where either separator is unambiguous on the next
// parse). TrimSpace first so a value with trailing whitespace never grows a
// double separator.
func appendAllowlistToken(hay, host string) string {
	v := strings.TrimSpace(hay)
	if v == "" {
		return host
	}
	sep := " "
	if strings.Contains(v, ",") {
		sep = ","
	}
	return v + sep + host
}

// hasToken reports whether needle is one of the tokens of hay, split on comma
// (the API's actual MOD_ALLOWED_EGRESS_HOSTS separator — see
// appendAllowlistToken) or whitespace (tolerated for a hand-authored or
// legacy space-separated value).
func hasToken(hay, needle string) bool {
	for _, field := range strings.Fields(hay) {
		for _, t := range strings.Split(field, ",") {
			if t == needle {
				return true
			}
		}
	}
	return false
}

// Q14=A (2026-10-12, OWNER-DECISIONS Round 47) — MOD local CA trust: every
// stack service that received a co-located MOD API host injection also gets
// the MOD local CA fetched at container start from
// http://<co-located host>/mod-local-ca.crt — the same trust-on-first-fetch
// the mesh sidecars and managed.go use (persistent_stacks _shared.py
// _trust_site_ca_at_start; managed.go managedCAPath). Fetch-at-start works on
// remote workers with no host bind, tracks CA rotation on restart, and a
// failed fetch just leaves no file (|| rm -f) so the app falls back to public
// roots.
//
// SSL_CERT_FILE/REQUESTS_CA_BUNDLE are NEVER pointed at the MOD CA alone —
// that would make every public TLS call (HuggingFace, PyPI, S3) fail once the
// vars are set, and point at a missing file if the fetch itself failed. The
// wrapper instead builds a merged bundle (first existing system CA bundle +
// the fetched MOD CA) at /mod-ca/bundle.pem and only exports the vars when
// that bundle is non-empty and the MOD CA fetch succeeded. The CA dir lives at
// /tmp/mod-ca (not /mod-ca) because root-level dirs are not writable by
// non-root image users (e.g. nginx-unprivileged uid 101), mirroring the mesh
// sidecars' /tmp/mesh-ca precedent, and the fetch probes wget/curl because
// slim images ship neither. NODE_EXTRA_CA_CERTS
// is additive (Node merges it with its built-in roots), so it's safe to point
// at the MOD CA file alone whenever the fetch succeeded.
// A service that already sets one of these three keys itself keeps its own
// value for that key (envHasKey) — an author's explicit choice always wins.
// A service with neither entrypoint nor command cannot be hooked at start
// without clobbering the image's own entrypoint (which we cannot re-exec),
// so it gets no CA.
const (
	modCAFile   = "/tmp/mod-ca/mod-local-ca.crt"
	modCADir    = "/tmp/mod-ca"
	modCABundle = "/tmp/mod-ca/bundle.pem"
)

// systemCABundlePaths are checked in order; the first that exists and is
// non-empty is copied as the base of the merged trust bundle.
var systemCABundlePaths = []string{
	"/etc/ssl/certs/ca-certificates.crt",
	"/etc/pki/tls/certs/ca-bundle.crt",
	"/etc/ssl/cert.pem",
}

// caHostSafe is precedent #2's guard: the host lands inside a shell string, so
// anything outside [A-Za-z0-9.-] gets no CA fetch.
func caHostSafe(host string) bool {
	if host == "" {
		return false
	}
	for i := 0; i < len(host); i++ {
		c := host[i]
		ok := (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
			(c >= '0' && c <= '9') || c == '.' || c == '-'
		if !ok {
			return false
		}
	}
	return true
}

// envHasKey reports whether the service's environment (either compose shape,
// merge-resolved) already carries key.
func envHasKey(svc *yaml.Node, key string) bool {
	env := resolveMergedMapping(mapGet(resolveMergedMapping(svc), "environment"))
	if env == nil {
		return false
	}
	if env.Kind == yaml.MappingNode {
		for i := 0; i+1 < len(env.Content); i += 2 {
			if env.Content[i].Value == key {
				return true
			}
		}
		return false
	}
	if env.Kind == yaml.SequenceNode {
		for _, item := range env.Content {
			k, found, _ := partitionOnce(item.Value, "=")
			if found && strings.TrimSpace(k) == key {
				return true
			}
		}
	}
	return false
}

// caWrapService adds the fetch-at-start CA step (merged system+MOD trust
// bundle) to one service, mirroring _trust_site_ca_at_start's
// `sh -c "...; exec <original>"` pattern: exec-form entrypoint/command become
// the compose `command` and the wrapper execs them via "$0" "$@"; a
// shell-form (scalar) entrypoint/command is embedded verbatim instead.
// Returns false when the service is left alone.
func caWrapService(svc *yaml.Node, host string) bool {
	if !caHostSafe(host) {
		return false
	}
	merged := resolveMergedMapping(svc)
	wantSSL := !envHasKey(merged, "SSL_CERT_FILE")
	wantReqBundle := !envHasKey(merged, "REQUESTS_CA_BUNDLE")
	wantNodeExtra := !envHasKey(merged, "NODE_EXTRA_CA_CERTS")
	wantCurlBundle := !envHasKey(merged, "CURL_CA_BUNDLE")
	if !wantSSL && !wantReqBundle && !wantNodeExtra && !wantCurlBundle {
		return false // author already pins every CA-trust var we'd set
	}
	ep := mapGet(merged, "entrypoint")
	cmd := mapGet(merged, "command")
	if ep == nil && cmd == nil {
		return false // no hook point without clobbering the image's entrypoint
	}
	epIsSeq := ep != nil && ep.Kind == yaml.SequenceNode
	epScalar := ep != nil && ep.Kind == yaml.ScalarNode && ep.Value != ""
	cmdIsSeq := cmd != nil && cmd.Kind == yaml.SequenceNode
	cmdScalar := cmd != nil && cmd.Kind == yaml.ScalarNode && cmd.Value != ""

	fetch := "mkdir -p " + modCADir + " 2>/dev/null; " +
		"if command -v wget >/dev/null 2>&1; then wget -q -T 10 -O " + modCAFile +
		" http://" + host + "/mod-local-ca.crt || rm -f " + modCAFile +
		"; elif command -v curl >/dev/null 2>&1; then curl -fsS -m 10 -o " + modCAFile +
		" http://" + host + "/mod-local-ca.crt || rm -f " + modCAFile +
		"; else rm -f " + modCAFile + "; fi; "
	// Merge system roots + the fetched MOD CA so public TLS endpoints keep
	// working; only attempted when the fetch produced a non-empty file.
	bundleBuild := "if [ -s " + modCAFile + " ]; then rm -f " + modCABundle +
		"; for f in " + strings.Join(systemCABundlePaths, " ") +
		// `$$f`: compose interpolates `$f` (unset → blank) before the shell sees it.
		"; do if [ -s \"$$f\" ]; then cp \"$$f\" " + modCABundle + "; break; fi; done" +
		"; cat " + modCAFile + " >> " + modCABundle + "; fi; "
	var exportLines string
	if wantSSL || wantReqBundle || wantCurlBundle {
		exportLines += "if [ -s " + modCAFile + " ] && [ -s " + modCABundle + " ]; then "
		if wantSSL {
			exportLines += "export SSL_CERT_FILE=" + modCABundle + "; "
		}
		if wantReqBundle {
			exportLines += "export REQUESTS_CA_BUNDLE=" + modCABundle + "; "
		}
		if wantCurlBundle {
			exportLines += "export CURL_CA_BUNDLE=" + modCABundle + "; "
		}
		exportLines += "fi; "
	}
	if wantNodeExtra {
		exportLines += "if [ -s " + modCAFile + " ]; then export NODE_EXTRA_CA_CERTS=" + modCAFile + "; fi; "
	}
	prefix := fetch + bundleBuild + exportLines

	var script string
	if epIsSeq || (!epScalar && cmdIsSeq) {
		script = prefix + "exec \"$0\" \"$@\""
		argv := &yaml.Node{Kind: yaml.SequenceNode}
		for _, n := range []*yaml.Node{ep, cmd} {
			if n == nil {
				continue
			}
			if n.Kind == yaml.SequenceNode {
				for _, item := range n.Content {
					argv.Content = append(argv.Content, item)
				}
			} else if n.Kind == yaml.ScalarNode && n.Value != "" {
				argv.Content = append(argv.Content, scalar(n.Value))
			}
		}
		if len(argv.Content) == 0 {
			return false
		}
		replaceOrAppend(svc, "command", argv)
	} else if epScalar {
		script = prefix + "exec " + ep.Value + " \"$@\""
		if cmdIsSeq {
			argv := &yaml.Node{Kind: yaml.SequenceNode}
			for _, item := range cmd.Content {
				argv.Content = append(argv.Content, item)
			}
			replaceOrAppend(svc, "command", argv)
		}
	} else if cmdScalar {
		script = prefix + "exec " + cmd.Value
	} else {
		return false
	}
	replaceOrAppend(svc, "entrypoint", &yaml.Node{Kind: yaml.SequenceNode,
		Content: []*yaml.Node{scalar("sh"), scalar("-c"), scalar(script)}})
	return true
}

// hostFromURLShapedValue extracts the bare hostname from v when v itself is a
// URL (http:// or https:// scheme) — the shape an app's own "where's the
// platform API" env var takes (CONSIGN_MOD_API_URL=https://dev-api...). A
// value that isn't URL-shaped (a bare token, a number, a path) yields nothing.
func hostFromURLShapedValue(v string) (string, bool) {
	s := strings.TrimSpace(v)
	low := strings.ToLower(s)
	if !strings.HasPrefix(low, "http://") && !strings.HasPrefix(low, "https://") {
		return "", false
	}
	h := apiHostFromURL(s)
	return h, h != ""
}

// publicAPIHost assembles the platform's own public API hostname from the three
// multi-domain routing parts the worker container carries in its bootstrap env
// (PROTOCOL + API_SUBDOMAIN + BASE_DOMAIN, e.g. https + api + example.com ->
// example.com). On a host whose worker apiURL is the internal manager
// name ("http://api") and whose stack services carry no platform URL in their
// `environment`, that public hostname was never a candidate, so the /etc/hosts
// loopback leak for it was never re-mapped (B86i-i). Returns "" unless ALL THREE
// parts are set and the assembled host passes the same shape guard the candidate
// loop uses (isSkippableCandidateHost) — a localhost/onprem config, an IP
// literal, or a part that smuggles a scheme/path contributes nothing.
func publicAPIHost(protocol, subdomain, base string) string {
	p := strings.TrimSpace(protocol)
	for len(p) > 0 && (p[len(p)-1] == ':' || p[len(p)-1] == '/') {
		p = p[:len(p)-1] // tolerate a PROTOCOL written as "https:"
	}
	s := strings.TrimSpace(subdomain)
	b := strings.TrimSpace(base)
	if p == "" || s == "" || b == "" {
		return ""
	}
	// An onprem/localhost config has no public API hostname at all — the API's
	// own assembly (email_unsubscribe._api_base_url) collapses that case to the
	// bare base domain, which is never a cross-container DNS leak.
	if strings.EqualFold(b, "localhost") || strings.EqualFold(b, "127.0.0.1") {
		return ""
	}
	dotted := s + "." + b
	h := apiHostFromURL(p + "://" + dotted)
	if h != dotted || isSkippableCandidateHost(h) {
		return ""
	}
	return h
}

// publicAPIHostFromEnv reads the worker's own bootstrap env — the three vars are
// host names only, never logged as values — and returns the public API hostname
// ("" when any part is unset).
func publicAPIHostFromEnv() string {
	return publicAPIHost(os.Getenv("PROTOCOL"), os.Getenv("API_SUBDOMAIN"), os.Getenv("BASE_DOMAIN"))
}

// isSkippableCandidateHost reports whether host must never become an
// extra_hosts entry: empty, a bare IP literal (nothing to re-map by
// hostname), or "localhost" (never a cross-container DNS leak).
func isSkippableCandidateHost(host string) bool {
	if host == "" {
		return true
	}
	if strings.EqualFold(host, "localhost") {
		return true
	}
	_, err := netip.ParseAddr(host)
	return err == nil
}

// collectEnvURLHosts adds every URL-shaped `environment` value's hostname for
// svc into out, covering both the mapping (`environment: {K: v}`) and list
// (`environment: ["K=v"]`) compose shapes. B86i-c — a service (or its
// `environment` block) that shares config via a YAML merge key
// (`<<: *anchor`) is resolved first: yaml.v3 never expands merge keys when
// unmarshaling into a raw *yaml.Node (that resolution is a decode-into-struct
// feature — see decode.go's `unmarshal`), so a candidate host living only
// behind an anchor was previously invisible here even though `docker compose`
// (and the API's own Python yaml.safe_load render) resolve it fine — the
// container ends up with the real env var and zero extra_hosts injected.
func collectEnvURLHosts(svc *yaml.Node, out map[string]bool) {
	env := mapGet(resolveMergedMapping(svc), "environment")
	env = resolveMergedMapping(env)
	if env == nil {
		return
	}
	switch env.Kind {
	case yaml.MappingNode:
		for i := 0; i+1 < len(env.Content); i += 2 {
			if h, ok := hostFromURLShapedValue(env.Content[i+1].Value); ok {
				out[h] = true
			}
		}
	case yaml.SequenceNode:
		for _, item := range env.Content {
			_, found, val := partitionOnce(item.Value, "=")
			if !found {
				continue
			}
			if h, ok := hostFromURLShapedValue(val); ok {
				out[h] = true
			}
		}
	}
}

// apiHostInjection records, per co-located host, which services received a
// new extra_hosts entry — the caller logs one line per host.
type apiHostInjection struct {
	Host     string
	Services []string
}

// injectAPIHostYAML is the pure decision: given a compose YAML, the worker's
// own apiURL hostname, and a resolver callback (host -> resolved address
// strings), it returns the rewritten YAML, the per-host injections made, and
// an error only on a YAML parse failure.
//
// The candidate-host set is apiHost (may be empty) UNION every hostname
// parsed from a URL-shaped `environment` value across every service — an app
// typically targets a platform URL it was configured with, not the worker's
// own apiURL. IP literals, "localhost", and anything the resolver doesn't
// report as loopback are left byte-for-byte alone.
//
// B86i-i — extraHosts (variadic, may be empty) lets the executor entry point
// seed the platform's own PUBLIC API hostname (PROTOCOL/API_SUBDOMAIN/BASE_DOMAIN
// from the worker's bootstrap env) into the same candidate set: on a host whose
// worker apiURL is the internal manager name ("http://api") and whose stack
// services carry no platform URL in their `environment`, the public hostname was
// previously never a candidate, so its /etc/hosts loopback leak was never re-mapped.
func injectAPIHostYAML(composeYAML, apiHost string,
	lookup func(host string) []string, extraHosts ...string) (string, []apiHostInjection, error) {
	var root yaml.Node
	if err := yaml.Unmarshal([]byte(composeYAML), &root); err != nil {
		// Ship as-is: compose's own parse error is the author's to see.
		return composeYAML, nil, fmt.Errorf("parse compose for api-host injection: %w", err)
	}
	if len(root.Content) == 0 || root.Content[0].Kind != yaml.MappingNode {
		return composeYAML, nil, nil
	}
	doc := root.Content[0]
	svcs := mapGet(doc, "services")
	if svcs == nil || svcs.Kind != yaml.MappingNode {
		return composeYAML, nil, nil
	}

	candidates := map[string]bool{}
	if h := strings.TrimSpace(apiHost); h != "" {
		candidates[h] = true
	}
	for _, a := range extraHosts {
		if h := strings.TrimSpace(a); h != "" {
			candidates[h] = true
		}
	}
	for i := 0; i+1 < len(svcs.Content); i += 2 {
		svc := svcs.Content[i+1]
		if svc == nil || svc.Kind != yaml.MappingNode {
			continue
		}
		collectEnvURLHosts(svc, candidates)
	}

	// Only a co-located host (this host's own /etc/hosts maps it to loopback)
	// needs the fix. The resolver is a parameter so this is unit-testable.
	var loopbackHosts []string
	for h := range candidates {
		if isSkippableCandidateHost(h) {
			continue
		}
		for _, a := range lookup(h) {
			if isLoopbackAddr(a) {
				loopbackHosts = append(loopbackHosts, h)
				break
			}
		}
	}
	if len(loopbackHosts) == 0 {
		return composeYAML, nil, nil
	}
	sort.Strings(loopbackHosts)

	touchedSet := map[string]bool{}
	perHost := map[string][]string{}
	caDone := map[string]bool{}
	for _, h := range loopbackHosts {
		entry := h + ":host-gateway"
		for i := 0; i+1 < len(svcs.Content); i += 2 {
			svc := svcs.Content[i+1]
			if svc == nil || svc.Kind != yaml.MappingNode {
				continue
			}
			name := svcs.Content[i].Value
			if extraHostsHas(svc, h) {
				// The author already pinned that hostname — respect their
				// choice, but still admit it to an advisory allowlist.
				allowlistAppend(svc, h)
				continue
			}
			// A netns joiner (`network_mode: service:<x>` / `container:<x>`) shares
			// its owner's /etc/hosts and docker rejects extra_hosts on it
			// ("conflicting options: custom host-to-IP mapping and the network
			// mode"); the owner is a service of the same stack and gets the entry.
			if !sharesNetns(svc) {
				eh := mapGet(svc, "extra_hosts")
				if eh != nil && eh.Kind == yaml.SequenceNode {
					eh.Content = append(eh.Content, scalar(entry))
				} else {
					// Missing, null, or a non-sequence value: replace with a sequence.
					replaceOrAppend(svc, "extra_hosts", &yaml.Node{Kind: yaml.SequenceNode,
						Content: []*yaml.Node{scalar(entry)}})
				}
			}
			allowlistAppend(svc, h)
			touchedSet[name] = true
			perHost[h] = append(perHost[h], name)
			// Q14=A — same services that got the loopback re-map also get MOD
			// local CA trust (fetch-at-start + SSL_CERT_FILE/REQUESTS_CA_BUNDLE).
			// One CA fetch per service: the first co-located host wins; every
			// co-located host serves the same manager CA.
			if !caDone[name] && caWrapService(svc, h) {
				caDone[name] = true
			}
		}
	}
	if len(touchedSet) == 0 {
		return composeYAML, nil, nil
	}
	out, err := yaml.Marshal(&yaml.Node{Kind: yaml.DocumentNode,
		Content: []*yaml.Node{doc}})
	if err != nil {
		return composeYAML, nil, fmt.Errorf("marshal compose after api-host injection: %w", err)
	}
	var injections []apiHostInjection
	for _, h := range loopbackHosts {
		if len(perHost[h]) > 0 {
			injections = append(injections, apiHostInjection{Host: h, Services: perHost[h]})
		}
	}
	return string(out), injections, nil
}

// sharesNetns reports whether a service joins another container's network
// namespace (`network_mode: service:<x>` or `container:<x>`).
func sharesNetns(svc *yaml.Node) bool {
	nm := mapGet(resolveMergedMapping(svc), "network_mode")
	if nm == nil {
		return false
	}
	v := strings.TrimSpace(nm.Value)
	return strings.HasPrefix(v, "service:") || strings.HasPrefix(v, "container:")
}

// isMergeKey reports whether a mapping key node is YAML's `<<` merge key.
func isMergeKey(k *yaml.Node) bool {
	return k != nil && k.Kind == yaml.ScalarNode && (k.Tag == "!!merge" || k.Value == "<<")
}

// mergeSources resolves a merge value to the mapping node(s) it references:
// a single alias (`<<: *a`), or a sequence of them (`<<: [*a, *b]`).
func mergeSources(v *yaml.Node) []*yaml.Node {
	if v == nil {
		return nil
	}
	if v.Kind == yaml.AliasNode {
		return []*yaml.Node{v.Alias}
	}
	if v.Kind == yaml.SequenceNode {
		var out []*yaml.Node
		for _, item := range v.Content {
			if item.Kind == yaml.AliasNode {
				out = append(out, item.Alias)
			} else {
				out = append(out, item)
			}
		}
		return out
	}
	return []*yaml.Node{v}
}

// resolveMergedMapping returns a flattened, read-only copy of a YAML mapping
// node with any `<<: *anchor` merge key(s) expanded into literal pairs. m
// itself (and the document it lives in) is never mutated — callers that need
// to WRITE a change still target the original node, so a merge-authored
// service's structure survives byte-for-byte in the output compose; only
// candidate detection needs to see what the merge resolves to. Per YAML merge
// semantics: a mapping's own explicit pairs win over anything merged in, and
// for `<<: [*a, *b]` the earlier source wins a key conflict between merges.
// A mapping without a merge key is returned unchanged (nil for anything else).
func resolveMergedMapping(m *yaml.Node) *yaml.Node {
	if m == nil || m.Kind != yaml.MappingNode {
		return m
	}
	hasMerge := false
	for i := 0; i+1 < len(m.Content); i += 2 {
		if isMergeKey(m.Content[i]) {
			hasMerge = true
			break
		}
	}
	if !hasMerge {
		return m
	}
	merged := map[string]*yaml.Node{}
	var order []string
	set := func(k, v *yaml.Node, overwrite bool) {
		key := k.Value
		if _, exists := merged[key]; !exists {
			order = append(order, key)
		} else if !overwrite {
			return
		}
		merged[key] = v
	}
	for i := 0; i+1 < len(m.Content); i += 2 {
		k, v := m.Content[i], m.Content[i+1]
		if !isMergeKey(k) {
			continue
		}
		for _, src := range mergeSources(v) {
			resolved := resolveMergedMapping(src)
			if resolved == nil || resolved.Kind != yaml.MappingNode {
				continue
			}
			for j := 0; j+1 < len(resolved.Content); j += 2 {
				set(resolved.Content[j], resolved.Content[j+1], false)
			}
		}
	}
	for i := 0; i+1 < len(m.Content); i += 2 {
		k, v := m.Content[i], m.Content[i+1]
		if isMergeKey(k) {
			continue
		}
		set(k, v, true)
	}
	out := &yaml.Node{Kind: yaml.MappingNode}
	for _, key := range order {
		out.Content = append(out.Content, &yaml.Node{Kind: yaml.ScalarNode, Value: key}, merged[key])
	}
	return out
}

// replaceOrAppend sets key to value in a mapping node, appending the pair when
// the key is absent (a `key:` with a null value is replaced in place).
func replaceOrAppend(m *yaml.Node, key string, value *yaml.Node) {
	for i := 0; i+1 < len(m.Content); i += 2 {
		if m.Content[i].Value == key {
			m.Content[i+1] = value
			return
		}
	}
	m.Content = append(m.Content, scalar(key), value)
}

// resolveAPIHostAddrsAttempts/Backoff bound the retry added by B86i-e below —
// a few hundred ms total, dwarfed by the rest of a stack deploy, never worth
// a platform-settings knob.
const (
	resolveAPIHostAddrsAttempts = 3
	resolveAPIHostAddrsBackoff  = 150 * time.Millisecond
)

// lookupIPAddr is resolveAPIHostAddrs' DNS lookup, a package var (not a
// method/closure) so a test can substitute a fake resolver — e.g. one that
// answers NXDOMAIN on its first call and loopback on a later one, the exact
// B86i-f race — without a real network and without threading a new field
// through Executor for this single call site.
var lookupIPAddr = net.DefaultResolver.LookupIPAddr

// resolveAPIHostAddrs resolves the API hostname on THIS host. The worker
// process sees the same /etc/hosts (and the same DNS leak) the stack
// containers inherit, so a loopback result here is exactly the leak B86i
// describes.
//
// B86i-e — stack 114's co-located fix never fired on its first live deploy:
// the worker container itself had been recreated ~38s earlier (a fresh
// network attach, Docker Desktop/WSL2's embedded-DNS host-proxy not
// necessarily warm yet), the single un-retried net.LookupIP here hit
// whatever transient resolver error that produced, and ANY lookup error was
// silently folded into "doesn't resolve, nothing to inject" — identical to
// the legitimate negative, with zero log line either way (resolveAPIHostAddrs
// had no logger and `injectStackAPIHost`'s own log lines only cover a YAML
// parse error and a successful injection).
//
// B86i-f — the B86i-e retry STILL never fired on stack 114's next redeploy
// (task 51352, 70s after a fresh `my_mod_worker` restart): zero "co-located"
// lines AND zero "api-host resolve failed" Warn lines, even though the real
// task payload parses and would have injected (proven by feeding the actual
// task_payload.compose_spec bytes through injectAPIHostYAML in isolation —
// it produces the injection every time). The only place left that returns
// with NO log line at all was B86i-e's own "definitive no such host, don't
// bother retrying" fast path below: right after a fresh network attach,
// Docker Desktop/WSL2's embedded DNS (127.0.0.11) can answer a single query
// with NXDOMAIN before its forwarder to the host's own hosts-file override
// has warmed up, THEN answer correctly a few hundred ms later (confirmed:
// `docker exec my_mod_worker getent hosts example.com` resolves
// to 127.0.0.1 cleanly once steady-state) — so a single NXDOMAIN right after
// worker startup is not actually "definitive" in this environment, it's the
// same race B86i-e thought it had closed. Treating it as retryable (like any
// other resolver error) lets a later attempt see the real answer; a host
// that is GENUINELY unregistered still ends up with every attempt returning
// NXDOMAIN, so the eventual behavior (no injection) is unchanged — only the
// timing-sensitive false negative is fixed. The genuine-negative conclusion
// now also logs (Debug — it is the overwhelmingly common non-co-located
// case, so it must not flood Info/Warn), so this path can never again look
// identical, from `docker logs my_mod_worker`, to "injection succeeded".
func (e *Executor) resolveAPIHostAddrs(ctx context.Context, host string) []string {
	var lastErr error
	for attempt := 1; attempt <= resolveAPIHostAddrsAttempts; attempt++ {
		addrs, err := lookupIPAddr(ctx, host)
		if err == nil {
			out := make([]string, 0, len(addrs))
			for _, a := range addrs {
				out = append(out, a.IP.String())
			}
			return out
		}
		lastErr = err
		if attempt < resolveAPIHostAddrsAttempts {
			select {
			case <-ctx.Done():
				lastErr = ctx.Err()
				attempt = resolveAPIHostAddrsAttempts // stop retrying, fall through to log
			case <-time.After(resolveAPIHostAddrsBackoff):
			}
		}
	}
	// Every attempt failed. A definitive NXDOMAIN on every single attempt is
	// the legitimate "this host really isn't co-located" negative — expected
	// on nearly every deploy, so it only logs at Debug. Anything else
	// (timeout, temporary resolver error, context deadline) is NOT a
	// confident "no fix needed" — log it at Warn so a human diagnosing a
	// still-unreachable co-located API from the host sees a reason instead
	// of silence.
	var dnsErr *net.DNSError
	if errors.As(lastErr, &dnsErr) && dnsErr.IsNotFound {
		if e.log != nil {
			e.log.Debug("api-host resolve: no such host after retries", "host", host, "attempts", resolveAPIHostAddrsAttempts)
		}
		return []string{}
	}
	if e.log != nil {
		e.log.Warn("api-host resolve failed after retries", "host", host, "attempts", resolveAPIHostAddrsAttempts, "err", lastErr)
	}
	return []string{}
}

// injectStackAPIHost is the executor entry point: it never fails the deploy —
// on any error it logs and returns the spec unchanged.
func (e *Executor) injectStackAPIHost(ctx context.Context, cb Callbacks, apiURL, composeYAML string) string {
	host := apiHostFromURL(apiURL)
	// B86i-i — the platform's own public API hostname is a candidate too: on this
	// host the worker's apiURL is the internal manager name ("http://api", host-
	// local, resolves fine) and most stack services carry no platform URL in their
	// `environment`, so without this the public host never enters the set and its
	// /etc/hosts loopback leak is never re-mapped. Empty when any of the three
	// bootstrap parts is unset; a duplicate of an existing candidate is a no-op
	// (the candidate set is a map) and isSkippableCandidateHost still filters it.
	out, injections, err := injectAPIHostYAML(composeYAML, host, func(h string) []string {
		return e.resolveAPIHostAddrs(ctx, h)
	}, publicAPIHostFromEnv())
	if err != nil {
		// B86i-e — this must ALSO reach `docker logs my_mod_worker`, not just
		// cb.OnLogBatch: for a DirectTransport (embedded) worker — the stack
		// 114 deploy's shape — that callback writes straight to Redis with a
		// 24h TTL and never touches the worker's own stdout (see
		// worker_tasks.py stream_task_log's own comment on the split), so a
		// silent parse failure here was INDISTINGUISHABLE from "nothing to
		// inject" when diagnosing from the host: zero log line either way.
		// The success path already learned this lesson (see the loop below);
		// the error path hadn't.
		if e.log != nil {
			e.log.Error("api-host injection skipped", "err", err)
		}
		if cb.OnLogBatch != nil {
			cb.OnLogBatch([]string{fmt.Sprintf("api-host injection skipped: %v", err)})
		}
		return composeYAML
	}
	if len(injections) == 0 {
		// B86i-f — covers every other silent early return inside
		// injectAPIHostYAML (no services, no URL-shaped candidate host, no
		// candidate resolved loopback, or every candidate was already
		// user-pinned via extra_hosts): none of those are errors, so they
		// must not be Warn/Error noise on the overwhelmingly common
		// not-co-located deploy, but they must still reach `docker logs
		// my_mod_worker` at some level — the whole point of this bug was
		// that "nothing to inject" and "injection silently failed" were
		// indistinguishable from the host.
		if e.log != nil {
			e.log.Debug("api-host injection: nothing to inject", "api_host", host)
		}
	}
	for _, inj := range injections {
		// B86i-c — this must ALSO reach the worker's own stdout log
		// (`docker logs my_mod_worker`), not just cb.OnLogBatch: that callback
		// only calls tp.SendLog (the task's own log stream, see
		// executeStackTask in cmd/worker/main.go), which is invisible to
		// anyone diagnosing from the host. Hostname only — never log an env
		// value, which could carry an embedded credential.
		if e.log != nil {
			e.log.Info("co-located MOD API host injected", "host", inj.Host, "services", strings.Join(inj.Services, ", "))
		}
		if cb.OnLogBatch != nil {
			cb.OnLogBatch([]string{fmt.Sprintf(
				"co-located MOD API: %s resolves to loopback on this host — injected %s:host-gateway into service(s) %s",
				inj.Host, inj.Host, strings.Join(inj.Services, ", "))})
		}
	}
	return out
}
