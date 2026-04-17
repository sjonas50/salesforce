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
