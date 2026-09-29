{{/*
Expand the name of the chart.
*/}}
{{- define "tankarr.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name (63 characters, a DNS name limit).
*/}}
{{- define "tankarr.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "tankarr.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "tankarr.labels" -}}
helm.sh/chart: {{ include "tankarr.chart" . }}
{{ include "tankarr.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "tankarr.selectorLabels" -}}
app.kubernetes.io/name: {{ include "tankarr.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
The Secret the container loads its secret variables from.
*/}}
{{- define "tankarr.secretName" -}}
{{- default (include "tankarr.fullname" .) .Values.existingSecret }}
{{- end }}

{{/*
The image reference: the tag defaults to the chart's appVersion.
*/}}
{{- define "tankarr.image" -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) }}
{{- end }}
