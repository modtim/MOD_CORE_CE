package main

import "testing"

func TestShouldRunOnK8sDispatch(t *testing.T) {
	cases := []struct {
		mode    bool
		backend string
		want    bool
	}{
		{true, "k8s", true},
		{true, "K8S", true},
		{true, " k8s ", true},
		{true, "docker", false},
		{true, "", false}, // older API: fail closed to docker
		{true, "kubernetes", false},
		{false, "k8s", false}, // docker-only worker never runs k8s
		{false, "", false},
	}
	for _, c := range cases {
		if got := shouldRunOnK8s(c.mode, c.backend); got != c.want {
			t.Errorf("shouldRunOnK8s(%v, %q) = %v, want %v", c.mode, c.backend, got, c.want)
		}
	}
}
