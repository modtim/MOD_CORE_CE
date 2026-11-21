package executor

import (
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// Reproducer for the 401 on stage repro:
//   image: example.com/tenant-1/png-to-pdf-converter:latest
//   platform creds: admin@registry.example.com  (wrong registry → 401)
//   tenant creds:   robot$mod-push-1@example.com
// Picker must return the tenant creds.

var (
	platformH = transport.HarborCredentials{
		Registry: "ghcr.io",
		Username: "admin",
		Password: "platform-pass",
	}
	tenantH = transport.HarborCredentials{
		Registry: "example.com",
		Username: "robot$mod-push-1",
		Password: "tenant-pass",
	}
)

func TestPickRegistryAuth(t *testing.T) {
	cases := []struct {
		name     string
		image    string
		platform transport.HarborCredentials
		tenant   transport.HarborCredentials
		wantUser string
	}{
		{
			name:     "tenant image, tenant creds available",
			image:    "example.com/tenant-1/png-to-pdf-converter:latest",
			platform: platformH,
			tenant:   tenantH,
			wantUser: "robot$mod-push-1",
		},
		{
			name:     "platform image, both available — pick platform",
			image:    "ghcr.io/modtim/base-python:latest",
			platform: platformH,
			tenant:   tenantH,
			wantUser: "admin",
		},
		{
			name:     "tenant image, no tenant creds — fall back to platform (and 401, but visibly)",
			image:    "example.com/tenant-1/foo:latest",
			platform: platformH,
			tenant:   transport.HarborCredentials{},
			wantUser: "admin",
		},
		{
			name:     "tenant registry stored with https:// scheme — normalize and match",
			image:    "example.com/tenant-1/foo:latest",
			platform: platformH,
			tenant: transport.HarborCredentials{
				Registry: "https://example.com/",
				Username: "robot$mod-push-1",
				Password: "tenant-pass",
			},
			wantUser: "robot$mod-push-1",
		},
		{
			name:     "local registry image with port — no host match either side",
			image:    "registry:5000/tenant-1/foo:latest",
			platform: platformH,
			tenant:   tenantH,
			wantUser: "admin",
		},
		{
			name:     "docker hub image (no host segment) — platform fallback",
			image:    "alpine:3.18",
			platform: platformH,
			tenant:   tenantH,
			wantUser: "admin",
		},
		{
			name:     "namespaced docker hub image (no dot in first segment) — platform fallback",
			image:    "library/alpine:3.18",
			platform: platformH,
			tenant:   tenantH,
			wantUser: "admin",
		},
		{
			name:     "tenant Harbor mismatch on hostname — platform creds (and that pull will 401, intentionally)",
			image:    "other-registry.example.com/something/foo:latest",
			platform: platformH,
			tenant:   tenantH,
			wantUser: "admin",
		},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := PickRegistryAuth(c.image, c.platform, c.tenant)
			if got.Username != c.wantUser {
				t.Fatalf("PickRegistryAuth(%q) → user=%q want %q",
					c.image, got.Username, c.wantUser)
			}
		})
	}
}

func TestImageHost(t *testing.T) {
	cases := map[string]string{
		"example.com/tenant-1/foo:latest": "example.com",
		"ghcr.io/modtim/foo:latest":       "ghcr.io",
		"registry:5000/tenant-1/foo:latest":                "registry:5000",
		"localhost/foo:latest":                             "localhost",
		"alpine:3.18":                                      "",
		"library/alpine:3.18":                              "",
		"":                                                 "",
	}
	for in, want := range cases {
		if got := imageHost(in); got != want {
			t.Errorf("imageHost(%q) = %q want %q", in, got, want)
		}
	}
}

func TestNormalizeHost(t *testing.T) {
	cases := map[string]string{
		"https://example.com/": "example.com",
		"http://localhost:8080":                 "localhost:8080",
		"example.com":          "example.com",
		"example.com/":         "example.com",
		"":                                      "",
	}
	for in, want := range cases {
		if got := normalizeHost(in); got != want {
			t.Errorf("normalizeHost(%q) = %q want %q", in, got, want)
		}
	}
}

func TestRegistryKindFor(t *testing.T) {
	cases := []struct {
		name  string
		image string
		want  string
	}{
		{"tenant", "example.com/tenant-1/foo:latest", "tenant"},
		{"platform", "ghcr.io/modtim/foo:latest", "platform"},
		{"unknown registry, platform creds present", "other.example/x:latest", "platform"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := registryKindFor(c.image, platformH, tenantH); got != c.want {
				t.Fatalf("registryKindFor(%q) = %q want %q", c.image, got, c.want)
			}
		})
	}
	t.Run("no creds at all", func(t *testing.T) {
		if got := registryKindFor("foo:latest", transport.HarborCredentials{}, transport.HarborCredentials{}); got != "none" {
			t.Fatalf("expected 'none', got %q", got)
		}
	})
}
