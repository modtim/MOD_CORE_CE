{{/*
MOD Core chart — shared helpers.
*/}}

{{- define "mod.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "mod.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* Common labels */}}
{{- define "mod.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/version: {{ .Chart.AppVersion }}
app.kubernetes.io/part-of: mod
{{- end -}}

{{/* Labels for a named component (also used by selectors, so keep stable) */}}
{{- define "mod.componentLabels" -}}
{{ include "mod.labels" .root }}
app.kubernetes.io/name: mod-{{ .component }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/*
KAI scheduler name. Empty (no schedulerName rendered) unless kai.enabled.
Usage:
  {{- $sched := include "mod.schedulerName" . }}
  {{- if $sched }}
  schedulerName: {{ $sched }}
  {{- end }}
*/}}
{{- define "mod.schedulerName" -}}
{{- if .Values.kai.enabled -}}
{{- required "kai.schedulerName must be set when kai.enabled" .Values.kai.schedulerName -}}
{{- end -}}
{{- end -}}

{{/*
Image reference for a MOD component.
  include "mod.imageRef" (dict "image" .Values.api.image "global" .Values.global "chartAppVersion" .Chart.AppVersion)
Defaults: registry/project from global; tag = image.tag, else global.imageTag,
else the chart appVersion (global.imageTag is how the CE values pin one release tag).
*/}}
{{- define "mod.imageRef" -}}
{{- $registry := default .global.imageRegistry .image.registry -}}
{{- $project := default .global.imageProject .image.project -}}
{{- $tag := default (default .chartAppVersion .global.imageTag) .image.tag -}}
{{- printf "%s/%s/%s:%s" $registry $project .image.name $tag -}}
{{- end -}}

{{/* imagePullSecrets block (shared) */}}
{{- define "mod.imagePullSecrets" -}}
{{- with .Values.global.imagePullSecrets }}
imagePullSecrets:
{{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{/* ================= datastore helpers =================
Each datastore (postgres, valkey, versitygw, qdrant, keycloak, vault) is
either internal=true (StatefulSet rendered by this chart, headless service
<fullname>-<name>) or external (operator-provided host/port + secretRef).
All credential material lives in pre-existing Secrets — this chart renders
no Secret objects and values.yaml carries no secret values.
*/}}

{{- define "mod.postgres.host" -}}{{ if .Values.postgres.internal }}{{ include "mod.fullname" . }}-postgres{{ else }}{{ required "postgres.external.host is required when postgres.internal=false" .Values.postgres.external.host }}{{ end }}{{- end -}}
{{- define "mod.postgres.port" -}}{{ if .Values.postgres.internal }}5432{{ else }}{{ .Values.postgres.external.port }}{{ end }}{{- end -}}
{{- define "mod.postgres.secret" -}}{{ if .Values.postgres.internal }}{{ .Values.postgres.secret.name }}{{ else }}{{ required "postgres.external.secretRef is required when postgres.internal=false" .Values.postgres.external.secretRef }}{{ end }}{{- end -}}

{{- define "mod.valkey.host" -}}{{ if .Values.valkey.internal }}{{ include "mod.fullname" . }}-valkey{{ else }}{{ required "valkey.external.host is required when valkey.internal=false" .Values.valkey.external.host }}{{ end }}{{- end -}}
{{- define "mod.valkey.port" -}}{{ if .Values.valkey.internal }}6379{{ else }}{{ .Values.valkey.external.port }}{{ end }}{{- end -}}
{{- define "mod.valkey.secret" -}}{{ if .Values.valkey.internal }}{{ .Values.valkey.secret.name }}{{ else }}{{ required "valkey.external.secretRef is required when valkey.internal=false" .Values.valkey.external.secretRef }}{{ end }}{{- end -}}

{{- define "mod.versitygw.host" -}}{{ if .Values.versitygw.internal }}{{ include "mod.fullname" . }}-versitygw{{ else }}{{ required "versitygw.external.host is required when versitygw.internal=false" .Values.versitygw.external.host }}{{ end }}{{- end -}}
{{- define "mod.versitygw.port" -}}{{ if .Values.versitygw.internal }}7070{{ else }}{{ .Values.versitygw.external.port }}{{ end }}{{- end -}}
{{- define "mod.versitygw.secret" -}}{{ if .Values.versitygw.internal }}{{ .Values.versitygw.secret.name }}{{ else }}{{ required "versitygw.external.secretRef is required when versitygw.internal=false" .Values.versitygw.external.secretRef }}{{ end }}{{- end -}}

{{- define "mod.qdrant.host" -}}{{ if .Values.qdrant.internal }}{{ include "mod.fullname" . }}-qdrant{{ else }}{{ required "qdrant.external.host is required when qdrant.internal=false" .Values.qdrant.external.host }}{{ end }}{{- end -}}
{{- define "mod.qdrant.port" -}}{{ if .Values.qdrant.internal }}6333{{ else }}{{ .Values.qdrant.external.port }}{{ end }}{{- end -}}
{{- define "mod.qdrant.secret" -}}{{ if .Values.qdrant.internal }}{{ .Values.qdrant.secret.name }}{{ else }}{{ required "qdrant.external.secretRef is required when qdrant.internal=false" .Values.qdrant.external.secretRef }}{{ end }}{{- end -}}

{{/* keycloak internal URL: http(s)://host:port */}}
{{- define "mod.keycloak.internalUrl" -}}{{ if .Values.keycloak.internal }}http://{{ include "mod.fullname" . }}-keycloak:8080{{ else }}{{ required "keycloak.external.internalUrl is required when keycloak.internal=false" .Values.keycloak.external.internalUrl }}{{ end }}{{- end -}}
{{- define "mod.keycloak.secret" -}}{{ if .Values.keycloak.internal }}{{ .Values.keycloak.secret.name }}{{ else }}{{ required "keycloak.external.secretRef is required when keycloak.internal=false" .Values.keycloak.external.secretRef }}{{ end }}{{- end -}}
{{/* External hostname/issuer for clients (browser-reachable). Defaults to
    <keycloakSubdomain>.<baseDomain> when internal. */}}
{{- define "mod.keycloak.externalUrl" -}}{{ if .Values.keycloak.externalUrl }}{{ .Values.keycloak.externalUrl }}{{ else }}{{ .Values.global.protocol }}://{{ .Values.keycloak.keycloakSubdomain }}.{{ .Values.global.baseDomain }}{{ end }}{{- end -}}

{{/* CLI/env prefix of the secrets server image: `vault` (HashiCorp, default) or `bao` (OpenBao, CE). */}}
{{- define "mod.vault.cli" -}}{{ default "vault" .Values.vault.cli }}{{- end -}}

{{- define "mod.vault.addr" -}}{{ if .Values.vault.internal }}http://{{ include "mod.fullname" . }}-vault:8200{{ else }}{{ required "vault.external.addr is required when vault.internal=false" .Values.vault.external.addr }}{{ end }}{{- end -}}

{{/*
Security admission blocks (GY.C9.s4) — the kube-bench "restricted"
vocabulary for every MOD deployment/DaemonSet this chart renders, split by
WHERE Kubernetes actually accepts each field:

mod.podSecurity  -> ONLY real PodSecurityContext fields: seccompProfile.
                    Rendered under spec.template.spec.securityContext (children
                    at 8 spaces). allowPrivilegeEscalation, readOnlyRootFilesystem
                    and capabilities are NOT PodSecurityContext fields - emitting
                    them there makes strict decoding (helm install --wait / k3s
                    provisioning) reject the Deployment with
                    "unknown field spec.template.spec.securityContext.allowPrivilegeEscalation".
mod.containerSecurity -> ONLY real Container/Deployment SecurityContext fields:
                    allowPrivilegeEscalation false, readOnlyRootFilesystem true,
                    capabilities.drop = ALL. Rendered under a container's own
                    securityContext (children at 12 spaces), which is where the
                    restricted profile actually counts.

CORE/release/bench-report.sh scores all four strings over the rendered api,worker
output, so both helpers must stay in the api template for the score to hold.

uid policy is NOT part of either block: the MOD images each need their own
(api/dashboard listen on :80 and have no non-root USER; the worker must be
root for the host docker socket), so runAsNonRoot/runAsUser stay per
template. The worker DaemonSet deliberately includes neither block.

Usage (pod level, inside spec.template.spec.securityContext, children at 8):
    securityContext:
      runAsNonRoot: false
      {{- include "mod.podSecurity" . | nindent 8 }}
Usage (container level, inside a container's securityContext, children at 12):
      containers:
        - name: api
          securityContext:
            {{- include "mod.containerSecurity" . | nindent 12 }}
*/}}
{{- define "mod.podSecurity" -}}
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "mod.containerSecurity" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop:
    - ALL
{{- end -}}

{{/* API k8s backend: set when the worker runs in executor mode */}}
{{- define "mod.k8sBackend" -}}{{ eq .Values.worker.mode "executor" }}{{- end -}}
