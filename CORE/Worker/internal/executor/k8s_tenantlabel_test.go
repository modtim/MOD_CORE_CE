package executor

import "testing"

// W9: task pods carry mod.tech/tenant-id so a tenant stack namespace's
// isolation NetworkPolicy admits only that tenant's task pods.

func TestK8sTenantLabelFromTask(t *testing.T) {
	task := tsTask(map[string]string{"TENANT_ID": "99"}) // container_env value is NOT authoritative
	task.TenantID = 7
	spec, err := TaskSpecFromTask(task, tsCreds, TaskSpecOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if spec.Env[EnvTenantID] != "7" {
		t.Fatalf("MOD_TENANT_ID = %q, want 7", spec.Env[EnvTenantID])
	}
	b, _ := newTestK8s(t)
	_, job := submitK8s(t, b, spec)
	if got := job.Spec.Template.Labels[LabelTenantID]; got != "7" {
		t.Fatalf("pod tenant label = %q, want 7 (labels %v)", got, job.Spec.Template.Labels)
	}
	if job.Spec.Template.Labels[DefaultTaskPodSelectorKey] != DefaultTaskPodSelectorValue {
		t.Fatalf("task-pod selector label missing: %v", job.Spec.Template.Labels)
	}
}

func TestK8sTenantLabelFallbackAndSanitise(t *testing.T) {
	b, _ := newTestK8s(t)
	_, job := submitK8s(t, b, &TaskSpec{Image: "x", Env: map[string]string{"TENANT_ID": " Acme_Corp "}})
	if got := job.Spec.Template.Labels[LabelTenantID]; got != "acme-corp" {
		t.Fatalf("sanitised tenant label = %q, want acme-corp", got)
	}
}

func TestK8sTenantLabelOmittedWhenUnknown(t *testing.T) {
	b, _ := newTestK8s(t)
	_, job := submitK8s(t, b, &TaskSpec{Image: "x", Env: map[string]string{"TENANT_ID": "  "}})
	if v, ok := job.Spec.Template.Labels[LabelTenantID]; ok {
		t.Fatalf("tenant label must be omitted when unknown (fail-closed), got %q", v)
	}
	spec, err := TaskSpecFromTask(tsTask(nil), tsCreds, TaskSpecOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := spec.Env[EnvTenantID]; ok {
		t.Fatal("MOD_TENANT_ID must not be set when task.TenantID is 0")
	}
}
