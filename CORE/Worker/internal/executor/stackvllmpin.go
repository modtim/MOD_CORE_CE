package executor

// megaplan §14.7 — vLLM ignores Docker's device pinning for how much VRAM it
// is willing to grab: by default it pre-allocates a fraction of the WHOLE
// card's memory (--gpu-memory-utilization, default 0.90), so a "shared" card
// assignment (several stacks pinned to the same physical GPU, each under a
// VRAM_LIMIT) still lets vLLM grab most of the card for itself and starves
// its neighbours. Capping the flag to the assignment's actual share fixes
// that; it only applies when the assignment is a real fraction of a known
// card (Mode=="shared" && VRAMLimitMB>0 && a card's size is known) — an
// "exclusive" pin or an unknown card size leaves vLLM's own default alone.
//
// llama.cpp (`llama-server`) has no equivalent flag — it maps model layers to
// GPU explicitly via -ngl/--tensor-split, not a memory-fraction cap — so
// those services are left untouched (see llamaCppCommand below).

import (
	"strconv"
	"strings"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
	"gopkg.in/yaml.v3"
)

const vllmMemUtilFlag = "--gpu-memory-utilization"

// pinVLLMMemUtil appends the memory-utilization cap to every vLLM service's
// command, skipping services that already set the flag and llama.cpp
// services (there is nothing to change, but they are reported back so the
// caller can log why). Returns the rewritten spec, the services changed, and
// the llama.cpp services left alone. Spec is returned unchanged on any parse
// problem, no assignment, or when the assignment doesn't express a known
// fractional VRAM share.
func pinVLLMMemUtil(spec string, a *transport.GPUAssignment) (string, []string, []string) {
	if a == nil || a.Mode != "shared" || a.VRAMLimitMB <= 0 {
		return spec, nil, nil
	}
	cardMB := 0
	for _, c := range a.Cards {
		if c.CardVRAMMB > 0 && (cardMB == 0 || c.CardVRAMMB < cardMB) {
			cardMB = c.CardVRAMMB
		}
	}
	if cardMB <= 0 {
		return spec, nil, nil
	}
	frac := float64(a.VRAMLimitMB) / float64(cardMB)
	if frac > 0.95 {
		frac = 0.95
	}
	flag := vllmMemUtilFlag + "=" + strconv.FormatFloat(frac, 'f', 2, 64)

	var doc yaml.Node
	if err := yaml.Unmarshal([]byte(spec), &doc); err != nil || len(doc.Content) == 0 {
		return spec, nil, nil
	}
	services := mapValue(doc.Content[0], "services")
	if services == nil || services.Kind != yaml.MappingNode {
		return spec, nil, nil
	}
	var changed, llamaCpp []string
	for i := 0; i+1 < len(services.Content); i += 2 {
		name, svc := services.Content[i].Value, services.Content[i+1]
		if svc.Kind != yaml.MappingNode {
			continue
		}
		cmdText := serviceCommandText(svc)
		if llamaCppCommand(cmdText) {
			llamaCpp = append(llamaCpp, name)
			continue
		}
		if !isVLLMService(svc, cmdText) {
			continue
		}
		if strings.Contains(cmdText, vllmMemUtilFlag) {
			continue
		}
		// A service matched only by image name (no `command:`/`entrypoint:`
		// override) bakes its args into the image CMD, which compose YAML
		// cannot see or extend — inventing a bare `command:` here would
		// REPLACE that baked-in CMD instead of adding to it. Nothing safe to
		// append to, so leave it alone.
		if mapValue(svc, "command") == nil {
			continue
		}
		appendCommandArg(svc, flag)
		changed = append(changed, name)
	}
	if len(changed) == 0 {
		return spec, nil, llamaCpp
	}
	out, err := yaml.Marshal(&doc)
	if err != nil {
		return spec, nil, nil
	}
	return string(out), changed, llamaCpp
}

// isVLLMService detects a vLLM server: its image names vllm, or its
// command/entrypoint invokes vllm's serve CLI.
func isVLLMService(svc *yaml.Node, cmdText string) bool {
	if image := mapValue(svc, "image"); image != nil && strings.Contains(strings.ToLower(image.Value), "vllm") {
		return true
	}
	lower := strings.ToLower(cmdText)
	return strings.Contains(lower, "vllm") &&
		(strings.Contains(lower, "serve") || strings.Contains(lower, "api_server"))
}

func llamaCppCommand(cmdText string) bool {
	return strings.Contains(strings.ToLower(cmdText), "llama-server")
}

// serviceCommandText flattens a service's `command:` and `entrypoint:`
// (either scalar-string or list form) into one lowercase-searchable string.
func serviceCommandText(svc *yaml.Node) string {
	var parts []string
	for _, key := range []string{"command", "entrypoint"} {
		n := mapValue(svc, key)
		if n == nil {
			continue
		}
		switch n.Kind {
		case yaml.ScalarNode:
			parts = append(parts, n.Value)
		case yaml.SequenceNode:
			for _, item := range n.Content {
				parts = append(parts, item.Value)
			}
		}
	}
	return strings.Join(parts, " ")
}

// appendCommandArg adds one token to a service's existing `command:`,
// matching whichever form (list or scalar string) is already there. Callers
// must confirm a `command:` node exists first — inventing one from nothing
// would replace the image's baked-in CMD instead of adding to it.
func appendCommandArg(svc *yaml.Node, arg string) {
	cmd := mapValue(svc, "command")
	switch cmd.Kind {
	case yaml.SequenceNode:
		cmd.Content = append(cmd.Content, &yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: arg})
	default:
		cmd.Value = strings.TrimSpace(cmd.Value + " " + arg)
	}
}
