{{/* Chart name */}}
{{- define "sentinel.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Fully qualified app name */}}
{{- define "sentinel.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s" (include "sentinel.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/* Common labels */}}
{{- define "sentinel.labels" -}}
app.kubernetes.io/name: {{ include "sentinel.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: sentinel
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/* Image ref: pass the component image name (e.g. "api", "mcp-k8s") */}}
{{- define "sentinel.image" -}}
{{- printf "%s/%s:%s" .root.Values.image.registry .name .root.Values.image.tag -}}
{{- end -}}

{{/* Secret name (existing or chart-managed) */}}
{{- define "sentinel.secretName" -}}
{{- if .Values.secrets.existingSecret -}}
{{- .Values.secrets.existingSecret -}}
{{- else -}}
{{- printf "%s-secrets" (include "sentinel.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "sentinel.observerSA" -}}
{{- printf "%s-observer" (include "sentinel.fullname" .) -}}
{{- end -}}

{{- define "sentinel.actuatorSA" -}}
{{- printf "%s-actuator" (include "sentinel.fullname" .) -}}
{{- end -}}

{{/* Postgres host: in-cluster service when we deploy it, else external */}}
{{- define "sentinel.postgresHost" -}}
{{- if .Values.postgres.deploy -}}postgres{{- else -}}{{ .Values.postgres.external.host }}{{- end -}}
{{- end -}}

{{- define "sentinel.postgresPort" -}}
{{- if .Values.postgres.deploy -}}5432{{- else -}}{{ .Values.postgres.external.port }}{{- end -}}
{{- end -}}

{{/* Redis URL: in-cluster or external */}}
{{- define "sentinel.redisUrl" -}}
{{- if .Values.redis.deploy -}}redis://redis:6379/0{{- else -}}{{ .Values.redis.external.url }}{{- end -}}
{{- end -}}

{{/* Qdrant URL: in-cluster or external */}}
{{- define "sentinel.qdrantUrl" -}}
{{- if .Values.qdrant.deploy -}}http://qdrant:6333{{- else -}}{{ .Values.qdrant.external.url }}{{- end -}}
{{- end -}}
