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

{{/* Langfuse host: explicit override, else in-cluster service, else external */}}
{{- define "sentinel.langfuseHost" -}}
{{- if .Values.tracing.langfuseHost -}}{{ .Values.tracing.langfuseHost }}
{{- else if .Values.langfuse.deploy -}}http://langfuse-web:3000
{{- else -}}{{ .Values.langfuse.external.host }}
{{- end -}}
{{- end -}}

{{/* Redis connection string for Langfuse: same in-cluster/external host as the
     main app, but its own logical DB index so state never collides. */}}
{{- define "sentinel.langfuseRedisUrl" -}}
{{- if .Values.redis.deploy -}}redis://redis:6379/2{{- else -}}{{ .Values.redis.external.url }}{{- end -}}
{{- end -}}

{{/* Init container: waits for the shared postgres to accept connections, then
     idempotently creates the `langfuse` logical database. Requires the pod's
     envFrom to already include the chart Secret (for POSTGRES_PASSWORD). */}}
{{- define "sentinel.langfuseDbInitContainer" -}}
- name: langfuse-db-init
  image: postgres:15-alpine
  env:
    - name: POSTGRES_PASSWORD
      valueFrom: { secretKeyRef: { name: {{ include "sentinel.secretName" . }}, key: POSTGRES_PASSWORD } }
  command: ["sh", "-c"]
  args:
    - |
      set -e
      for i in $(seq 1 60); do
        if PGPASSWORD="$POSTGRES_PASSWORD" pg_isready -h {{ include "sentinel.postgresHost" . }} -p {{ include "sentinel.postgresPort" . }} -U {{ .Values.postgres.user }} >/dev/null 2>&1; then
          if PGPASSWORD="$POSTGRES_PASSWORD" psql -h {{ include "sentinel.postgresHost" . }} -p {{ include "sentinel.postgresPort" . }} -U {{ .Values.postgres.user }} -d {{ .Values.postgres.database }} -tc "SELECT 1 FROM pg_database WHERE datname = 'langfuse'" | grep -q 1; then
            exit 0
          fi
          PGPASSWORD="$POSTGRES_PASSWORD" createdb -h {{ include "sentinel.postgresHost" . }} -p {{ include "sentinel.postgresPort" . }} -U {{ .Values.postgres.user }} langfuse && exit 0
        fi
        sleep 2
      done
      echo "postgres unreachable or langfuse database creation failed after retries" >&2
      exit 1
{{- end -}}
