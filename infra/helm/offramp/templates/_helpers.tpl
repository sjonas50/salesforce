{{/* Common labels */}}
{{- define "offramp.labels" -}}
app.kubernetes.io/name: offramp
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
offramp.attic.ai/customer: {{ .Values.customer.alias }}
{{- end -}}

{{/* Selector labels */}}
{{- define "offramp.selectorLabels" -}}
app.kubernetes.io/name: offramp
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* Per-component name */}}
{{- define "offramp.componentName" -}}
{{- $component := .component -}}
{{- printf "%s-%s" .Release.Name $component | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Pod-level security context. Runs every Off-Ramp workload as a non-root,
unprivileged user with the runtime default seccomp profile.
*/}}
{{- define "offramp.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
fsGroup: 10001
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{/*
Container-level security context. The root filesystem is read-only, so every
container also mounts the "tmp" emptyDir at /tmp and points uv's cache there
(see "offramp.tmpVolume" / "offramp.tmpVolumeMount").
*/}}
{{- define "offramp.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
privileged: false
capabilities:
  drop: [ALL]
{{- end -}}

{{/* Writable scratch space for containers with a read-only root filesystem. */}}
{{- define "offramp.tmpVolume" -}}
- name: tmp
  emptyDir: {}
{{- end -}}

{{- define "offramp.tmpVolumeMount" -}}
- name: tmp
  mountPath: /tmp
{{- end -}}

{{- define "offramp.tmpEnv" -}}
- name: TMPDIR
  value: /tmp
- name: UV_CACHE_DIR
  value: /tmp/uv-cache
{{- end -}}
