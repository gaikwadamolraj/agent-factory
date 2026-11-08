{{/*
Shared helpers of the agent-factory chart.
Two namespaces: the release namespace (agent-factory: the ATS services) and .Values.infra.namespace (agent-infra:
shared platform infrastructure). Every object states its namespace explicitly.
Selectors only use app.kubernetes.io/name (+ component on pod templates).
*/}}

{{/*
ats.chartLabels: Helm bookkeeping labels, added to every object (never to selectors or pod templates).
Input: root context.
*/}}
{{- define "ats.chartLabels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}

{{/*
ats.labels: object labels = optional name/component + part-of + chart labels.
Input: dict root, appName (optional), component (optional), partOf (optional, default global.partOf).
*/}}
{{- define "ats.labels" -}}
{{- with .appName -}}
app.kubernetes.io/name: {{ . }}
{{ end -}}
{{- with .component -}}
app.kubernetes.io/component: {{ . }}
{{ end -}}
app.kubernetes.io/part-of: {{ .partOf | default .root.Values.global.partOf }}
{{ include "ats.chartLabels" .root }}
{{- end }}

{{/*
ats.metadata: name, namespace and labels of an object.
Input: dict root, name, namespace (optional, default: release namespace), appName (optional),
       component (optional), partOf (optional), annotations (optional map).
*/}}
{{- define "ats.metadata" -}}
name: {{ .name }}
namespace: {{ .namespace | default .root.Release.Namespace }}
labels:
  {{- include "ats.labels" . | nindent 2 }}
{{- with .annotations }}
annotations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end }}

{{/*
ats.infraMetadata: ats.metadata for an object in the infra namespace (part-of = the infra namespace name).
Input: dict root, name, appName (optional), annotations (optional map).
*/}}
{{- define "ats.infraMetadata" -}}
{{- include "ats.metadata" (merge (dict "namespace" .root.Values.infra.namespace "partOf" .root.Values.infra.namespace) .) }}
{{- end }}

{{/*
ats.podLabels: labels of a pod template (and of nothing else): name + optional component.
Input: dict appName, component (optional).
*/}}
{{- define "ats.podLabels" -}}
app.kubernetes.io/name: {{ .appName }}
{{- with .component }}
app.kubernetes.io/component: {{ . }}
{{- end }}
{{- end }}

{{/*
ats.imageTag: tag of an ats image: image.tag, else global.image.tag, else Chart.appVersion.
Input: dict root, image (map with repository, optional tag).
*/}}
{{- define "ats.imageTag" -}}
{{- .image.tag | default .root.Values.global.image.tag | default .root.Chart.AppVersion | toString -}}
{{- end }}

{{/*
ats.image: full image reference of an ats image.
registry: image.registry when the key is set (even ""), else global.image.registry; empty = no registry (k3d).
Input: dict root, image (map with repository, optional registry/tag).
*/}}
{{- define "ats.image" -}}
{{- $registry := .root.Values.global.image.registry | default "" -}}
{{- if hasKey .image "registry" }}{{ $registry = .image.registry | default "" }}{{ end -}}
{{- $tag := include "ats.imageTag" . -}}
{{- if $registry -}}
{{ printf "%s/%s:%s" (trimSuffix "/" $registry) .image.repository $tag }}
{{- else -}}
{{ printf "%s:%s" .image.repository $tag }}
{{- end -}}
{{- end }}

{{/*
ats.env: container env list. Each item is {name, value} or {name, secretKeyRef: {secret, key, optional}} where
secret is a key of .Values.secrets (platform, infra): Secrets are referenced by name, never rendered.
Plain values go through tpl, so they may use {{ .Values.infra.namespace }}.
Order is kept, so $(VAR) references to earlier entries keep working.
Input: dict root, env (list).
*/}}
{{- define "ats.env" -}}
{{- $root := .root -}}
{{- range .env }}
- name: {{ .name }}
  {{- if .secretKeyRef }}
  valueFrom:
    secretKeyRef:
      name: {{ required (printf "secrets.%s must name an existing Secret" .secretKeyRef.secret) (get $root.Values.secrets .secretKeyRef.secret) }}
      key: {{ .secretKeyRef.key }}
      {{- if hasKey .secretKeyRef "optional" }}
      optional: {{ .secretKeyRef.optional }}
      {{- end }}
  {{- else }}
  value: {{ tpl (toString .value) $root | quote }}
  {{- end }}
{{- end }}
{{- end }}

{{/*
ats.configMap: a ConfigMap with string data (stable name; consumers roll via a checksum/* pod annotation).
Values go through tpl, so they may use {{ .Values.infra.namespace }} (FQDNs of infra services).
Input: dict root, name, namespace (optional), partOf (optional), appName (optional label), data (map).
*/}}
{{- define "ats.configMap" -}}
apiVersion: v1
kind: ConfigMap
metadata:
  {{- include "ats.metadata" (dict "root" .root "name" .name "namespace" .namespace "partOf" .partOf "appName" .appName) | nindent 2 }}
data:
  {{- range $key, $value := .data }}
  {{ $key }}: {{ tpl (toString $value) $.root | quote }}
  {{- end }}
{{- end }}

{{/*
ats.checksum: sha256 of a rendered template, for checksum/* pod annotations.
Input: dict root, path (template path below templates/, e.g. "infra/llm-config/configmap.yaml").
*/}}
{{- define "ats.checksum" -}}
{{- include (print .root.Template.BasePath "/" .path) .root | sha256sum -}}
{{- end }}

{{/*
ats.service: ClusterIP Service selecting pods by app.kubernetes.io/name.
Input: dict root, name, namespace (optional), partOf (optional), ports (list of {name, port, targetPort}).
*/}}
{{- define "ats.service" -}}
apiVersion: v1
kind: Service
metadata:
  {{- include "ats.metadata" (dict "root" .root "name" .name "namespace" .namespace "partOf" .partOf "appName" .name) | nindent 2 }}
spec:
  type: ClusterIP
  selector:
    app.kubernetes.io/name: {{ .name }}
  ports:
    {{- toYaml .ports | nindent 4 }}
{{- end }}

{{/*
ats.infraService: ats.service in the infra namespace.
Input: dict root, name, ports.
*/}}
{{- define "ats.infraService" -}}
{{- include "ats.service" (merge (dict "namespace" .root.Values.infra.namespace "partOf" .root.Values.infra.namespace) .) }}
{{- end }}

{{/*
ats.callersNetworkPolicy: NetworkPolicy that admits only the listed pods of the release namespace on one port.
Used by infra components (callers live in another namespace) and by services (same namespace).
Input: dict root, name, namespace (optional), partOf (optional), appName (pods it protects), port,
       callers (list of app.kubernetes.io/name values; empty = any pod of the release namespace),
       component (optional: admit pods with this app.kubernetes.io/component instead of callers),
       extraNamespaces (optional list of namespaces whose pods are all admitted, e.g. agent-infra).
*/}}
{{- define "ats.callersNetworkPolicy" -}}
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  {{- include "ats.metadata" (dict "root" .root "name" .name "namespace" .namespace "partOf" .partOf) | nindent 2 }}
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: {{ .appName }}
  policyTypes:
    - Ingress
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: {{ .root.Release.Namespace }}
          {{- if .component }}
          podSelector:
            matchLabels:
              app.kubernetes.io/component: {{ .component }}
          {{- else if .callers }}
          podSelector:
            matchExpressions:
              - key: app.kubernetes.io/name
                operator: In
                values:
                  {{- toYaml .callers | nindent 18 }}
          {{- end }}
        {{- range .extraNamespaces }}
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: {{ . }}
        {{- end }}
      ports:
        - protocol: TCP
          port: {{ .port }}
{{- end }}

{{/*
ats.serviceName: the object/app name of a service (agent-skill). Default: the kebab-case of its values key
(agentKnowledgeGraph -> agent-knowledge-graph, atsUi -> ats-ui); a block may override it with values.name.
Input: dict key (values map key), values (the service block).
*/}}
{{- define "ats.serviceName" -}}
{{- .values.name | default (.key | kebabcase) -}}
{{- end }}

{{/*
ats.configData: the effective ConfigMap data of a service as YAML: its .config, plus OAUTH_CLIENT_AUDIENCES
("caller=svc1|svc2,...", callers sorted by name) when it declares .oauthClients (the auth-service). Callers do
toYaml/fromYaml it. Used both to render the ConfigMap (config.yaml) and to checksum it (ats.configChecksums).
Input: dict config (map, optional), oauthClients (map, optional).
*/}}
{{- define "ats.configData" -}}
{{- $config := .config | default dict -}}
{{- if .oauthClients -}}
{{- $pairs := list -}}
{{- range $client, $audiences := .oauthClients -}}
{{- $pairs = append $pairs (printf "%s=%s" $client (join "|" $audiences)) -}}
{{- end -}}
{{- $config = merge (dict "OAUTH_CLIENT_AUDIENCES" (join "," $pairs)) $config -}}
{{- end -}}
{{- toYaml $config -}}
{{- end }}

{{/*
ats.configChecksums: checksum/* pod annotations for the chart-managed ConfigMaps a service reads via envFrom, so
the pod rolls when any of them changes. Each envFrom name maps to one annotation:
  auth-config -> checksum/auth-config   llm-config -> checksum/llm-config   <svc>-config -> checksum/config
The <svc>-config data is looked up in .all (services + agents merged) by name, so the knowledge-graph worker, which
reuses agent-knowledge-graph-config, rolls with it too. Values are hashes of the ConfigMap data, not of a file.
Input: dict root, all (merged service maps), envFrom (list of ConfigMap names).
*/}}
{{- define "ats.configChecksums" -}}
{{- $root := .root -}}
{{- $all := .all -}}
{{- $out := dict -}}
{{- range .envFrom -}}
{{- if eq . "auth-config" -}}
{{- $out = set $out "checksum/auth-config" (toYaml $root.Values.authConfig | sha256sum) -}}
{{- else if eq . "llm-config" -}}
{{- $out = set $out "checksum/llm-config" (toYaml $root.Values.infra.llmConfig.data | sha256sum) -}}
{{- else -}}
{{- $target := . -}}
{{- range $key, $svc := $all -}}
{{- if eq (printf "%s-config" (include "ats.serviceName" (dict "key" $key "values" $svc))) $target -}}
{{- $out = set $out "checksum/config" (include "ats.configData" (dict "config" $svc.config "oauthClients" $svc.oauthClients) | sha256sum) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if $out -}}
{{- toYaml $out -}}
{{- end -}}
{{- end }}

{{/*
ats.networkPolicy: a service NetworkPolicy driven by its values .networkPolicy block. Ingress admits callers of the
release namespace (same shape as ats.callersNetworkPolicy); egress is opt-in and keeps a fixed, readable order.
Input: dict
  root     root context
  name     object name (default <svc>-traffic)
  appName  pods it protects (app.kubernetes.io/name)
  port     the protected port (default: the service's first containerPort)
  ingress  dict: callers (list) | component (label); empty => any pod of the release namespace
  egress   optional dict:
             denyAll     true => egress: [] (no egress at all)
             dns         true => cluster DNS (kube-system, 53/udp+tcp)
             https       true => 443/tcp anywhere (external hosts without stable IPs)
             authService true => auth-service on its port (JWKS / service tokens)
             infraPeers  list of {appName, port} in infra.namespace
             releasePeers list of {appName, port} in the release namespace (e.g. MCP servers a proxy reaches)
             lgtm        true => lgtm OTLP (4317 + 4318) in infra.namespace
*/}}
{{- define "ats.networkPolicy" -}}
{{- $root := .root -}}
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  {{- include "ats.metadata" (dict "root" $root "name" .name) | nindent 2 }}
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: {{ .appName }}
  policyTypes:
    - Ingress
    {{- if .egress }}
    - Egress
    {{- end }}
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: {{ $root.Release.Namespace }}
          {{- if .ingress.component }}
          podSelector:
            matchLabels:
              app.kubernetes.io/component: {{ .ingress.component }}
          {{- else if .ingress.callers }}
          podSelector:
            matchExpressions:
              - key: app.kubernetes.io/name
                operator: In
                values:
                  {{- toYaml .ingress.callers | nindent 18 }}
          {{- end }}
      ports:
        - protocol: TCP
          port: {{ .port }}
  {{- with .egress }}
  {{- if .denyAll }}
  egress: []
  {{- else }}
  egress:
    {{- if .dns }}
    # DNS (cluster DNS in kube-system)
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: kube-system
      ports:
        - protocol: UDP
          port: 53
        - protocol: TCP
          port: 53
    {{- end }}
    {{- if .https }}
    # HTTPS anywhere: external hosts (e.g. model downloads) have no stable IPs
    - ports:
        - protocol: TCP
          port: 443
    {{- end }}
    {{- if .authService }}
    # auth-service: JWKS (once, and again only for an unknown key id)
    - to:
        - podSelector:
            matchLabels:
              app.kubernetes.io/name: auth-service
      ports:
        - protocol: TCP
          port: {{ (first $root.Values.services.authService.ports).containerPort }}
    {{- end }}
    {{- range .infraPeers }}
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: {{ $root.Values.infra.namespace }}
          podSelector:
            matchLabels:
              app.kubernetes.io/name: {{ .appName }}
      ports:
        - protocol: TCP
          port: {{ .port }}
    {{- end }}
    {{- range .releasePeers }}
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: {{ $root.Release.Namespace }}
          podSelector:
            matchLabels:
              app.kubernetes.io/name: {{ .appName }}
      ports:
        - protocol: TCP
          port: {{ .port }}
    {{- end }}
    {{- if .lgtm }}
    # traces, metrics and logs to lgtm
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: {{ $root.Values.infra.namespace }}
          podSelector:
            matchLabels:
              app.kubernetes.io/name: lgtm
      ports:
        - protocol: TCP
          port: 4317
        - protocol: TCP
          port: 4318
    {{- end }}
  {{- end }}
  {{- end }}
{{- end }}

{{/*
ats.serviceDeployment: Deployment of an ATS service, driven by its values block (.Values.services.<x>).
Input: dict
  root        root context
  name        object and app name (agent-skill)
  component   optional app.kubernetes.io/component label
  values      the service values: replicas, image, containerName, command, args, ports, env,
              podSecurityContext, securityContext (default global.containerSecurityContext), startupProbe,
              readinessProbe, livenessProbe, resources, volumeMounts, volumes, automountServiceAccountToken,
              enableServiceLinks, terminationGracePeriodSeconds, strategy
  image       optional image map overriding values.image (the knowledge-graph worker reuses that image)
  envFrom     list of ConfigMap names
  checksumAnnotations  pre-rendered "checksum/*: <hash>" YAML lines (see ats.configChecksums); pods roll on change
  replicas    false to omit spec.replicas (an autoscaler owns it)
*/}}
{{- define "ats.serviceDeployment" -}}
{{- $root := .root -}}
{{- $values := .values -}}
apiVersion: apps/v1
kind: Deployment
metadata:
  {{- include "ats.metadata" (dict "root" $root "name" .name "appName" .name "component" .component) | nindent 2 }}
spec:
  {{- if ne (toString .replicas) "false" }}
  replicas: {{ $values.replicas }}
  {{- end }}
  {{- with $values.strategy }}
  strategy:
    {{- toYaml . | nindent 4 }}
  {{- end }}
  selector:
    matchLabels:
      app.kubernetes.io/name: {{ .name }}
  template:
    metadata:
      labels:
        {{- include "ats.podLabels" (dict "appName" .name "component" .component) | nindent 8 }}
      {{- with .checksumAnnotations }}
      annotations:
        {{- . | nindent 8 }}
      {{- end }}
    spec:
      {{- if hasKey $values "automountServiceAccountToken" }}
      automountServiceAccountToken: {{ $values.automountServiceAccountToken }}
      {{- end }}
      {{- if hasKey $values "enableServiceLinks" }}
      enableServiceLinks: {{ $values.enableServiceLinks }}
      {{- end }}
      {{- with $values.terminationGracePeriodSeconds }}
      terminationGracePeriodSeconds: {{ . }}
      {{- end }}
      {{- with $values.podSecurityContext }}
      securityContext:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      containers:
        - name: {{ $values.containerName }}
          image: {{ include "ats.image" (dict "root" $root "image" (.image | default $values.image)) }}
          imagePullPolicy: {{ $root.Values.global.image.pullPolicy }}
          {{- with $values.command }}
          command:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          {{- with $values.args }}
          args:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          {{- with $values.ports }}
          ports:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          {{- with .envFrom }}
          envFrom:
            {{- range . }}
            - configMapRef:
                name: {{ . }}
            {{- end }}
          {{- end }}
          {{- with $values.env }}
          env:
            {{- include "ats.env" (dict "root" $root "env" .) | nindent 12 }}
          {{- end }}
          {{- with $values.startupProbe }}
          startupProbe:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          {{- with $values.readinessProbe }}
          readinessProbe:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          {{- with $values.livenessProbe }}
          livenessProbe:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          {{- with $values.resources }}
          resources:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          securityContext:
            {{- toYaml ($values.securityContext | default $root.Values.global.containerSecurityContext) | nindent 12 }}
          {{- with $values.volumeMounts }}
          volumeMounts:
            {{- toYaml . | nindent 12 }}
          {{- end }}
      {{- with $values.volumes }}
      volumes:
        {{- toYaml . | nindent 8 }}
      {{- end }}
{{- end }}
