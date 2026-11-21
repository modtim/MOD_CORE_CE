package executor

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tlsconf"
)

func TestDecideNodeAPIURL(t *testing.T) {
	cases := []struct {
		name    string
		url     string
		mode    string
		want    string
		wantErr bool
	}{
		{"off passes plain http through", "http://api:80", "off", "http://api:80", false},
		{"absent mode passes plain http through", "http://api:80", "", "http://api:80", false},
		{"report passes plain http through", "http://api:80", "report", "http://api:80", false},
		{"require refuses plain http", "http://api:80", "require", "", true},
		{"require refuses plain http with trailing slash", "http://api:80/", "require", "", true},
		{"require allows https", "https://example.com", "require", "https://example.com", false},
		{"off allows https unchanged", "https://example.com", "off", "https://example.com", false},
		{"empty url with require passes through", "", "require", "", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := decideNodeAPIURL(tc.url, tc.mode)
			if (err != nil) != tc.wantErr {
				t.Fatalf("decideNodeAPIURL(%q, %q) err = %v, wantErr %v", tc.url, tc.mode, err, tc.wantErr)
			}
			if got != tc.want {
				t.Fatalf("decideNodeAPIURL(%q, %q) = %q, want %q", tc.url, tc.mode, got, tc.want)
			}
			if tc.wantErr && !strings.Contains(err.Error(), "require") {
				t.Fatalf("refusal error must name the mode: %v", err)
			}
		})
	}
}

func TestInternalCABundleMount(t *testing.T) {
	t.Run("unset env yields nothing", func(t *testing.T) {
		t.Setenv(tlsconf.EnvVar, "")
		if p, b := internalCABundleMount(); p != "" || b != "" {
			t.Fatalf("unset env: got (%q, %q), want empty", p, b)
		}
	})

	t.Run("missing file yields nothing", func(t *testing.T) {
		t.Setenv(tlsconf.EnvVar, filepath.Join(t.TempDir(), "nope.pem"))
		if p, b := internalCABundleMount(); p != "" || b != "" {
			t.Fatalf("missing file: got (%q, %q), want empty", p, b)
		}
	})

	t.Run("readable file yields ro bind", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "ca-bundle.pem")
		if err := os.WriteFile(path, []byte("pem"), 0o600); err != nil {
			t.Fatal(err)
		}
		t.Setenv(tlsconf.EnvVar, "  "+path+"  ") // whitespace must be trimmed
		p, b := internalCABundleMount()
		if p != tlsconf.BundleContainerPath {
			t.Fatalf("container path = %q, want %q", p, tlsconf.BundleContainerPath)
		}
		want := path + ":" + tlsconf.BundleContainerPath + ":ro"
		if b != want {
			t.Fatalf("bind = %q, want %q", b, want)
		}
	})
}
