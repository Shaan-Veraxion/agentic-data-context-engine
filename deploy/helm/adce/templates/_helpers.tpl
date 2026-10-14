{{- define "adce.labels" -}}
app.kubernetes.io/part-of: adce
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}

{{- define "adce.secretName" -}}
{{- default (printf "%s-secrets" .Release.Name) .Values.secrets.existingSecret -}}
{{- end -}}

{{- define "adce.image" -}}
{{- $img := index .root.Values.image .kind -}}
{{ $img.repository }}:{{ $img.tag }}
{{- end -}}

{{- define "adce.env" -}}
envFrom:
  - configMapRef: { name: {{ .Release.Name }}-config }
  - secretRef: { name: {{ include "adce.secretName" . }} }
{{- end -}}
