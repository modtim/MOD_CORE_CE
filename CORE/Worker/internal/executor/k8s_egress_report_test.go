package executor

import (
	"reflect"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

func TestK8sEgressReport(t *testing.T) {
	hosts := []string{"api.openai.com", "*.anthropic.com"}
	cases := []struct {
		name     string
		spec     *TaskSpec
		fqdnMode string
		want     *transport.EgressReport
	}{
		{
			name:     "nil spec returns nil",
			spec:     nil,
			fqdnMode: FQDNModeCilium,
			want:     nil,
		},
		{
			name:     "default-deny returns nil",
			spec:     &TaskSpec{Egress: EgressDefaultDeny},
			fqdnMode: FQDNModeCilium,
			want:     nil,
		},
		{
			name:     "user-egress (open) returns nil",
			spec:     &TaskSpec{Egress: EgressUserEgress},
			fqdnMode: FQDNModeCilium,
			want:     nil,
		},
		{
			name:     "allowlist with cilium mode",
			spec:     &TaskSpec{Egress: EgressAllowlist, EgressHosts: hosts},
			fqdnMode: FQDNModeCilium,
			want: &transport.EgressReport{
				Mode:         "cilium",
				AllowedHosts: hosts,
			},
		},
		{
			name:     "allowlist with off mode is unenforced",
			spec:     &TaskSpec{Egress: EgressAllowlist, EgressHosts: hosts},
			fqdnMode: FQDNModeOff,
			want: &transport.EgressReport{
				Mode:         "unenforced",
				AllowedHosts: hosts,
			},
		},
		{
			name:     "allowlist with unknown mode is unenforced",
			spec:     &TaskSpec{Egress: EgressAllowlist, EgressHosts: hosts},
			fqdnMode: "something-else",
			want: &transport.EgressReport{
				Mode:         "unenforced",
				AllowedHosts: hosts,
			},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := K8sEgressReport(tc.spec, tc.fqdnMode)
			if !reflect.DeepEqual(got, tc.want) {
				t.Fatalf("K8sEgressReport() = %#v, want %#v", got, tc.want)
			}
		})
	}
}
