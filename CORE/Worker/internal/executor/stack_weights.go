package executor

import (
	"context"
	"fmt"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/weights"
	"gopkg.in/yaml.v3"
)

const hfOfflineVar = "HF_HUB_OFFLINE"

// fetchWeights is weights.Fetch, kept behind a package-level var so tests
// can stub the registry round-trip.
var fetchWeights = weights.Fetch

// toWeightRefs maps the task payload's weight refs to the weights package's
// Ref type (identical JSON shape, distinct Go types).
func toWeightRefs(refs []transport.WeightRef) []weights.Ref {
	if refs == nil {
		return nil
	}
	out := make([]weights.Ref, len(refs))
	for i, r := range refs {
		files := make([]weights.File, len(r.Files))
		for j, f := range r.Files {
			files[j] = weights.File{Path: f.Path, SHA256: f.SHA256, Size: f.Size}
		}
		out[i] = weights.Ref{
			ArtifactRef: r.ArtifactRef,
			RepoID:      r.RepoID,
			Revision:    r.Revision,
			Files:       files,
		}
	}
	return out
}

// prepareStackWeights materializes the task's weight refs into the local HF
// cache, and — only when every ref fetched — flips the compose spec to fully
// offline weight resolution. When the task carries no weights it is a
// byte-for-byte no-op: the fetch is never called and the spec is returned
// unchanged. An error names the repo (never credentials).
func prepareStackWeights(ctx context.Context, p *transport.TaskPayload, compose string, creds *transport.Credentials, logf func(...string)) (string, error) {
	if len(p.Weights) == 0 {
		return compose, nil
	}
	refs := toWeightRefs(p.Weights)
	var wcreds transport.HarborCredentials
	if creds != nil {
		wcreds = PickRegistryAuth(p.Weights[0].ArtifactRef, creds.Harbor, creds.TenantHarbor)
	}
	fetched, err := fetchWeights(ctx, hfCachePath(), refs, wcreds)
	if err != nil {
		return compose, fmt.Errorf("materialize weights: %w", err)
	}
	if !fetched {
		// Partial fetch: leave the stack online — it falls back to resolving
		// whatever weights are missing from the network. Only an error fails
		// the deploy (weights.Fetch reports a partial result as an error).
		if logf != nil {
			logf("weights: fetch incomplete — stack stays online (no HF_HUB_OFFLINE)")
		}
		return compose, nil
	}
	if logf != nil {
		msgs := make([]string, 0, len(refs))
		for _, r := range refs {
			msgs = append(msgs, fmt.Sprintf("weights: %s@%s ready (%d files)", r.RepoID, r.Revision, len(r.Files)))
		}
		logf(msgs...)
	}
	// GF.C0b.9 mirror interlock: record the source AT FETCH TIME — these
	// bytes came through the platform's own registry, so re-fetching them
	// after an eviction is a cheap platform pull, not a cold WAN download.
	// The on-box GC (modelcache_gc.go) evicts these before WAN-sourced
	// weights; best-effort, a marker failure never fails the deploy.
	for _, r := range refs {
		if rd, err := weights.RepoDir(hfCachePath(), r.RepoID); err == nil {
			markModelSource(rd, modelSourceMirror)
		}
	}
	return injectHFOffline(compose)
}

// injectHFOffline sets HF_HUB_OFFLINE=1 on every service's `environment` in a
// compose YAML, so model containers resolve weights purely from the local HF
// cache (primed by the task's weights refs) and never reach the network.
//
// Each service's `environment` may be a mapping, a list of "K=V" strings, or
// absent. All three forms are handled; an existing HF_HUB_OFFLINE (any value)
// is overridden to "1". Everything else is left untouched.
//
// Invalid YAML is an error. A document without a `services` mapping is
// returned unchanged with a nil error.
func injectHFOffline(composeYAML string) (string, error) {
	var root yaml.Node
	if err := yaml.Unmarshal([]byte(composeYAML), &root); err != nil {
		return composeYAML, fmt.Errorf("injectHFOffline: parse compose: %w", err)
	}
	if len(root.Content) == 0 || root.Content[0].Kind != yaml.MappingNode {
		return composeYAML, nil
	}
	doc := root.Content[0]
	svcs := mapGet(doc, "services")
	if svcs == nil || svcs.Kind != yaml.MappingNode {
		return composeYAML, nil
	}

	changed := false
	for i := 0; i+1 < len(svcs.Content); i += 2 {
		svc := svcs.Content[i+1]
		if svc.Kind != yaml.MappingNode {
			continue
		}
		if envNode := mapGet(svc, "environment"); envNode != nil {
			if touched := setEnvVar(envNode, hfOfflineVar, "1"); touched {
				changed = true
			}
			continue
		}
		// Absent: create it as a mapping with just the one var.
		env := mapping(scalar(hfOfflineVar), scalar("1"))
		svc.Content = append(svc.Content, scalar("environment"), env)
		changed = true
	}

	if !changed {
		return composeYAML, nil
	}
	out, err := yaml.Marshal(&root)
	if err != nil {
		return composeYAML, fmt.Errorf("injectHFOffline: re-marshal: %w", err)
	}
	return string(out), nil
}

// setEnvVar sets key=value inside an `environment` node, which is either a
// mapping or a list of "K=V" scalars. Returns true if the node changed.
// An existing key (any value) is overridden. A null/empty value is treated as
// an empty mapping/list so the var is added cleanly.
func setEnvVar(env *yaml.Node, key, value string) bool {
	switch env.Kind {
	case yaml.MappingNode:
		// envNode is a mapping; env may be a null node that Unmarshal left
		// empty (Kind MappingNode, no content) — treat as a fresh mapping.
		for i := 0; i+1 < len(env.Content); i += 2 {
			if env.Content[i].Value == key {
				env.Content[i+1] = scalar(value)
				return true
			}
		}
		env.Content = append(env.Content, scalar(key), scalar(value))
		return true
	case yaml.SequenceNode:
		for _, e := range env.Content {
			if e.Kind != yaml.ScalarNode {
				continue
			}
			if k, _, ok := cutEnvKV(e.Value); ok && k == key {
				e.Value = key + "=" + value
				return true
			}
		}
		env.Content = append(env.Content, scalar(key+"="+value))
		return true
	default:
		// Unexpected shape (e.g. a scalar or null). Replace with a mapping.
		env.Kind = yaml.MappingNode
		env.Tag = ""
		env.Value = ""
		env.Content = []*yaml.Node{scalar(key), scalar(value)}
		return true
	}
}

// setEnvVarIfAbsent is setEnvVar but never touches an existing key. Returns
// true only if the var was added.
func setEnvVarIfAbsent(env *yaml.Node, key, value string) bool {
	switch env.Kind {
	case yaml.MappingNode:
		for i := 0; i+1 < len(env.Content); i += 2 {
			if env.Content[i].Value == key {
				return false
			}
		}
		env.Content = append(env.Content, scalar(key), scalar(value))
		return true
	case yaml.SequenceNode:
		for _, e := range env.Content {
			if e.Kind != yaml.ScalarNode {
				continue
			}
			if k, _, ok := cutEnvKV(e.Value); ok && k == key {
				return false
			}
		}
		env.Content = append(env.Content, scalar(key+"="+value))
		return true
	default:
		// Null/empty environment (compose "environment:" with no value).
		// Replace with a mapping holding the one var.
		env.Kind = yaml.MappingNode
		env.Tag = ""
		env.Value = ""
		env.Content = []*yaml.Node{scalar(key), scalar(value)}
		return true
	}
}

// cutEnvKV splits a "K=V" environment list entry into its key and value.
func cutEnvKV(s string) (key, val string, ok bool) {
	for j := 0; j < len(s); j++ {
		if s[j] == '=' {
			return s[:j], s[j+1:], true
		}
	}
	return "", "", false
}
