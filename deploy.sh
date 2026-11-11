#!/usr/bin/env bash
# Build the ATS images and deploy the platform with Helm (chart deploy/helm/agent-factory, release "ats").
#
#   ./deploy.sh doctor                    # check what a deploy needs on this machine and say what is missing
#   ./deploy.sh init                      # once per cluster: create namespaces agent-factory + agent-infra,
#                                         #   print the Secret keys to create (and which are missing)
#   ./deploy.sh init --generate-secrets   # same, then create the missing GENERATED keys (signing key, client
#                                         #   secrets) in agent-factory-secrets; values are never printed
#   ./deploy.sh                           # build every image, then deploy (KEDA operator installed if missing)
#   ./deploy.sh agent-skill ats-ui        # build only these, deploy; the others keep their running tag
#   ./deploy.sh apply                     # no builds: re-deploy chart/config changes with the running tags
#   ./deploy.sh schema                    # build the schema image and deploy (the migration hook runs)
#   ./deploy.sh keda                      # install / upgrade the KEDA operator (Helm chart kedacore/keda)
#   ./deploy.sh migrate-to-helm [--dry-run]   # one-time switch from the old kustomize objects (see script)
#   ENV=prod REGISTRY=registry.example.com/ats ./deploy.sh ...   # values-prod.yaml, push to REGISTRY
#
# Local registry (dev): used AUTOMATICALLY when it is set up and the cluster can pull from it, which is much
# faster than `k3d image import`. One-time setup, then nothing to remember:
#   ./scripts/local_registry.sh        # creates it, joins the k3d network, trusts it on every node
#   ./deploy.sh agent-skill            # detects it, pushes one image, deploys, leaves the rest alone
# Falls back to `k3d image import` whenever it is missing or the cluster cannot pull from it.
# REGISTRY=none forces the import path.
#
# Components (= image names; grouped under services/<group>/<component>, see service_dir):
#   auth-service ats-orchestrator agent-skill agent-compute agent-sandbox agent-knowledge-graph agent-guardrail
#   agent-document registry-service ats-ui admin-ui schema
# agent-knowledge-graph also covers agent-knowledge-graph-worker (same image).
#
# Env: ENV (dev | prod, default dev), CLUSTER_NAME (k3d cluster, default my-cluster), IMAGE_TAG (default: timestamp),
#      REGISTRY (prod only: registry/prefix the images are pushed to).
# Unbuilt components keep their tag: the chart records every tag in ConfigMap image-tags, which this script
# reads back (see running_tag).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART="$ROOT/deploy/helm/agent-factory"
RELEASE="agent-factory"
APP_NAMESPACE="agent-factory"
INFRA_NAMESPACE="agent-infra"
PLATFORM_SECRET="agent-factory-secrets"
INFRA_SECRET="agent-infra-secrets"
KEDA_CHART_VERSION="2.20.2"
ENV="${ENV:-dev}"
CLUSTER_NAME="${CLUSTER_NAME:-my-cluster}"
# Where images are pushed, and where the cluster pulls them from. The two differ because the same registry
# has two names: `localhost:5000` to docker on this machine, `agent-factory-registry:5000` inside the k3d
# network. Leave both unset in dev and the registry is detected automatically (see use_local_registry);
# `REGISTRY=none` forces the `k3d image import` path, which needs no setup but copies every image into
# every node on every build.
REGISTRY="${REGISTRY:-}"
REGISTRY_PULL="${REGISTRY_PULL:-$REGISTRY}"
LOCAL_REGISTRY_NAME="${LOCAL_REGISTRY_NAME:-agent-factory-registry}"
LOCAL_REGISTRY_PORT="${LOCAL_REGISTRY_PORT:-5000}"
TAG="${IMAGE_TAG:-$(date +%Y%m%d-%H%M%S)}"

COMPONENTS=(auth-service ats-orchestrator agent-skill agent-compute agent-sandbox agent-knowledge-graph agent-guardrail
            agent-document registry-service ats-ui admin-ui schema)

# Required Secret keys (optional keys such as hf-key and grafana-* are not checked).
# client-secret-<caller>: one per caller in services.authService.oauthClients (values.yaml).
CLIENT_SECRET_KEYS=(client-secret-ats-orchestrator client-secret-ats-orchestrator-onboarding
                    client-secret-ats-orchestrator-bulk-match
                    client-secret-agent-knowledge-graph client-secret-registry-service
                    client-secret-agent-skill
                    client-secret-agent-document)
# agent-factory holds only app secrets + the connection details apps need to reach infra. Raw infra passwords live
# only in agent-infra-secrets. KEDA reuses database-url (no separate postgres login here).
PLATFORM_SECRET_KEYS=(database-url redis-url auth-signing-key "${CLIENT_SECRET_KEYS[@]}" llm-gateway-key
                      falkordb-password invite-signing-key)
INFRA_SECRET_KEYS=(postgres-user postgres-password postgres-db redis-password falkordb-password
                   master-key lmstudio-api-key vllm-qwen3-4b-api-key)
# Keys of agent-factory-secrets that `init --generate-secrets` can create (random, nothing to keep in sync by hand).
GENERATED_SECRET_KEYS=(auth-signing-key "${CLIENT_SECRET_KEYS[@]}" invite-signing-key)
# "<key in agent-factory-secrets>=<key in agent-infra-secrets>" pairs that must hold the same value (the only copies
# that cross namespaces; the passwords inside database-url/redis-url must match postgres-password/redis-password).
SHARED_SECRET_KEYS=(falkordb-password=falkordb-password llm-gateway-key=master-key)

# Both write to stderr. Progress is not output: when a function's stdout is captured - as the helm argument
# builders' once was - a log line becomes an argument, and helm then complains about its argument count
# instead of about the real problem. Diagnostics belong on stderr whatever the caller does with stdout.
log() { printf '\033[1;34m[deploy]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[deploy] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }
contains() { local item; for item in "${@:2}"; do [[ "$item" == "$1" ]] && return 0; done; return 1; }

# ------------------------------------------------------------------------------------------------ components
image_name() {  # docker image name of a component
  case "$1" in
    schema) echo "schema" ;;
    *) echo "$1" ;;
  esac
}

service_dir() {  # repo-relative folder of a component (its Dockerfile / build context lives here)
  case "$1" in
    ats-ui|ats-orchestrator) echo "services/ats/$1" ;;
    agent-skill|agent-compute|agent-sandbox|agent-knowledge-graph|agent-guardrail|agent-document) echo "services/agents/$1" ;;
    auth-service|registry-service|admin-ui|schema) echo "services/platform/$1" ;;
    *) die "no service directory for component '$1'" ;;
  esac
}

tag_key() {  # key of the component in ConfigMap image-tags (= its values key)
  case "$1" in
    auth-service) echo "authService" ;;
    ats-orchestrator) echo "atsOrchestrator" ;;
    agent-skill) echo "agentSkill" ;;
    agent-compute) echo "agentCompute" ;;
    agent-sandbox) echo "agentSandbox" ;;
    agent-knowledge-graph) echo "agentKnowledgeGraph" ;;
    agent-guardrail) echo "agentGuardrail" ;;
    agent-document) echo "agentDocument" ;;
    registry-service) echo "registryService" ;;
    ats-ui) echo "atsUi" ;;
    admin-ui) echo "adminUi" ;;
    schema) echo "schema" ;;
  esac
}

tag_value_path() {  # Helm value that sets the component's image tag (services.* vs agents.*: the two values maps)
  case "$1" in
    schema) echo "schema.image.tag" ;;
    agent-skill|agent-compute|agent-sandbox|agent-knowledge-graph|agent-guardrail|agent-document|registry-service) echo "agents.$(tag_key "$1").image.tag" ;;
    *) echo "services.$(tag_key "$1").image.tag" ;;
  esac
}

# ------------------------------------------------------------------------------------------------ checks
require_tools() {
  local tool
  for tool in "$@"; do command -v "$tool" >/dev/null || die "'$tool' is not installed"; done
}

refuse_old_kustomize_objects() {  # the old objects block a Helm install (same routes, old names)
  # ats-frontend existed only under the old kustomize layout (the Helm UI is now ats-ui), so it is a safe marker.
  if kubectl -n "$APP_NAMESPACE" get deployment ats-frontend >/dev/null 2>&1; then
    die "the old kustomize deployment is still running: run ./deploy.sh migrate-to-helm first"
  fi
}

secret_value() {  # base64 value of one Secret key (empty when missing); never printed
  kubectl -n "$1" get secret "$2" -o jsonpath="{.data.$3}" 2>/dev/null || true
}

missing_secret_keys() {  # prints "namespace/secret: key" for every missing key
  local namespace="$1" secret="$2" key
  for key in "${@:3}"; do
    [[ -n "$(secret_value "$namespace" "$secret" "$key")" ]] || echo "  $namespace/$secret: $key"
  done
}

all_missing_secret_keys() {  # missing keys of both Secrets, one per line
  missing_secret_keys "$APP_NAMESPACE" "$PLATFORM_SECRET" "${PLATFORM_SECRET_KEYS[@]}"
  missing_secret_keys "$INFRA_NAMESPACE" "$INFRA_SECRET" "${INFRA_SECRET_KEYS[@]}"
}

require_secrets() {  # stop early when a Secret or key is missing, or a shared value differs
  local missing pair platform_key infra_key different=""
  missing="$(all_missing_secret_keys)"
  [[ -z "$missing" ]] || die "missing Secret keys (run ./deploy.sh init for the list):
$missing"
  for pair in "${SHARED_SECRET_KEYS[@]}"; do
    platform_key="${pair%%=*}"
    infra_key="${pair#*=}"
    if [[ "$(secret_value "$APP_NAMESPACE" "$PLATFORM_SECRET" "$platform_key")" != \
          "$(secret_value "$INFRA_NAMESPACE" "$INFRA_SECRET" "$infra_key")" ]]; then
      different+="  $PLATFORM_SECRET:$platform_key != $INFRA_SECRET:$infra_key"$'\n'
    fi
  done
  [[ -z "$different" ]] || die "these Secret values must be equal in both namespaces:
$different"
}

# ------------------------------------------------------------------------------------------------ init
init_namespaces() {  # idempotent; labels match templates/infra/namespaces (Helm adopts the namespaces later)
  local namespace
  for namespace in "$APP_NAMESPACE" "$INFRA_NAMESPACE"; do
    kubectl create namespace "$namespace" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    kubectl label namespace "$namespace" "app.kubernetes.io/part-of=$namespace" --overwrite >/dev/null
    log "namespace $namespace ready"
  done
}

print_secret_instructions() {
  cat <<EOF

Create these Secrets once (values from your password manager, never committed):
  $APP_NAMESPACE/$PLATFORM_SECRET keys:  ${PLATFORM_SECRET_KEYS[*]}  (optional: hf-key)
  $INFRA_NAMESPACE/$INFRA_SECRET keys:  ${INFRA_SECRET_KEYS[*]}
                                       (optional: grafana-admin-user grafana-admin-password grafana-viewer-user grafana-viewer-password)
Generated keys (random; ./deploy.sh init --generate-secrets creates the missing ones without printing them):
  ${GENERATED_SECRET_KEYS[*]}
Optional during a signing-key rotation: auth-signing-key-previous (docs/security_auth.md)
Optional password pepper (auth-service): login-pepper-v1 (only when LOGIN_PEPPER_CURRENT_VERSION >= 1)
Templates with every key explained:
  deploy/helm/agent-factory/files/secret.example.agent-factory.yaml
  deploy/helm/agent-factory/files/secret.example.agent-infra.yaml
Same value in both Secrets: ${SHARED_SECRET_KEYS[*]}
Then run ./deploy.sh
EOF
  local missing
  missing="$(all_missing_secret_keys)"
  if [[ -n "$missing" ]]; then printf '\nMissing now:\n%s\n' "$missing"; else printf '\nAll required keys exist.\n'; fi
}

# ------------------------------------------------------------------------------------------------ generated secrets
generate_secret_value() {  # writes a new value for a generated key to file $2 (never to stdout)
  case "$1" in
    auth-signing-key) openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$2" 2>/dev/null ;;
    client-secret-*|invite-signing-key) openssl rand -hex 32 | tr -d '\n' > "$2" ;;
    *) die "no generator for Secret key '$1'" ;;
  esac
}

base64_one_line() { base64 < "$1" | tr -d '\n'; }   # GNU and BSD base64 alike

generate_missing_secrets() {  # create only the missing generated keys of agent-factory-secrets; prints key names only
  require_tools openssl
  local workdir key value_file patch_file created=()
  workdir="$(mktemp -d)"
  chmod 700 "$workdir"
  trap 'rm -rf "$workdir"; trap - RETURN' RETURN   # values exist on disk only while this function runs
  if ! kubectl -n "$APP_NAMESPACE" get secret "$PLATFORM_SECRET" >/dev/null 2>&1; then
    kubectl -n "$APP_NAMESPACE" create secret generic "$PLATFORM_SECRET" >/dev/null
    log "created empty Secret $APP_NAMESPACE/$PLATFORM_SECRET (add the other keys yourself)"
  fi
  for key in "${GENERATED_SECRET_KEYS[@]}"; do
    [[ -z "$(secret_value "$APP_NAMESPACE" "$PLATFORM_SECRET" "$key")" ]] || continue   # never overwrite
    value_file="$workdir/value"
    patch_file="$workdir/patch.json"
    generate_secret_value "$key" "$value_file"
    printf '{"data":{"%s":"%s"}}' "$key" "$(base64_one_line "$value_file")" > "$patch_file"
    # the value travels in a file, never on a command line (visible in the process list) or in the output
    kubectl -n "$APP_NAMESPACE" patch secret "$PLATFORM_SECRET" --type merge --patch-file "$patch_file" >/dev/null
    rm -f "$value_file" "$patch_file"
    created+=("$key")
  done
  if [[ ${#created[@]} -gt 0 ]]; then log "generated: ${created[*]}"; else log "all generated keys already exist"; fi
}

# ------------------------------------------------------------------------------------------- registry
use_local_registry() {  # in dev with no REGISTRY set: use the local registry when it is genuinely usable
  # Three things must hold, and all three are checked rather than assumed. A registry that this machine can
  # push to but the cluster cannot pull from is worse than no registry at all: every build would succeed and
  # every pod would then sit in ImagePullBackOff. When any check fails we fall back to `k3d image import`,
  # which always works, so a half-set-up registry costs time rather than a broken deploy.
  [[ "$ENV" == "dev" && -z "$REGISTRY" ]] || return 1
  command -v docker >/dev/null || return 1

  # 1. the registry answers on this machine
  docker exec "$LOCAL_REGISTRY_NAME" true >/dev/null 2>&1 || return 1
  # 2. it shares the cluster's docker network, so the nodes can resolve its name
  docker inspect "$LOCAL_REGISTRY_NAME" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' \
    2>/dev/null | grep -qw "k3d-${CLUSTER_NAME}" || return 1
  # 3. containerd is configured to trust it over plain http (it only reads registries.yaml at start, so an
  #    unconfigured node pulls over https, fails, and the deploy looks like a broken image)
  local node
  node="$(docker ps --format '{{.Names}}' | grep "^k3d-${CLUSTER_NAME}-" | grep -Ev 'serverlb|tools' | head -1)"
  [[ -n "$node" ]] || return 1
  docker exec "$node" sh -c "grep -q '${LOCAL_REGISTRY_NAME}:5000' /etc/rancher/k3s/registries.yaml 2>/dev/null" \
    || return 1

  REGISTRY="localhost:${LOCAL_REGISTRY_PORT}"
  REGISTRY_PULL="${LOCAL_REGISTRY_NAME}:5000"
  return 0
}

# ------------------------------------------------------------------------------------------- preflight
# Everything that has to be true *outside* the chart before a deploy works. These are host- and
# cluster-level facts a Helm chart cannot assert about itself - containerd's registry trust is a file on the
# node, read only at start, and Traefik's static config belongs to whoever installed Traefik. Each line here
# is a failure this project has actually hit, so `doctor` is the cheapest way to find out why a new machine
# behaves differently from this one.
DOCTOR_PROBLEMS=0
ok()    { printf '  \033[1;32m✓\033[0m %s\n' "$*"; }
bad()   { printf '  \033[1;31m✗\033[0m %s\n' "$*"; DOCTOR_PROBLEMS=$((DOCTOR_PROBLEMS + 1)); }
note()  { printf '    %s\n' "$*"; }

doctor() {
  printf '\n\033[1mTools\033[0m\n'
  local tool
  for tool in docker kubectl helm; do
    command -v "$tool" >/dev/null && ok "$tool" || bad "$tool is not installed"
  done

  printf '\n\033[1mCluster\033[0m\n'
  if kubectl version >/dev/null 2>&1; then
    ok "reachable ($(kubectl config current-context 2>/dev/null))"
  else
    bad "kubectl cannot reach a cluster"; note "k3d cluster create $CLUSTER_NAME"
  fi
  local namespace
  for namespace in "$APP_NAMESPACE" "$INFRA_NAMESPACE"; do
    kubectl get namespace "$namespace" >/dev/null 2>&1 \
      && ok "namespace $namespace" || bad "namespace $namespace is missing   (./deploy.sh init)"
  done
  keda_installed && ok "KEDA operator" || bad "KEDA is missing   (./deploy.sh keda)"

  printf '\n\033[1mSecrets\033[0m\n'
  local key missing=0
  for key in "${PLATFORM_SECRET_KEYS[@]}"; do
    kubectl -n "$APP_NAMESPACE" get secret "$PLATFORM_SECRET" -o jsonpath="{.data.$key}" 2>/dev/null \
      | grep -q . || missing=$((missing + 1))
  done
  [[ $missing -eq 0 ]] && ok "$PLATFORM_SECRET complete" \
    || bad "$PLATFORM_SECRET is missing $missing key(s)   (./deploy.sh init --generate-secrets)"

  printf '\n\033[1mImages\033[0m\n'
  if [[ "$REGISTRY" == "none" ]]; then
    ok "REGISTRY=none: k3d image import"
  elif use_local_registry; then
    ok "local registry $REGISTRY, cluster pulls $REGISTRY_PULL"
  elif [[ -n "$REGISTRY" ]]; then
    ok "registry $REGISTRY_PULL"
  else
    ok "no registry: k3d image import (slower)"
    note "faster: ./scripts/local_registry.sh"
    command -v k3d >/dev/null || bad "k3d is needed for the import path and is not installed"
  fi

  printf '\n\033[1mGateway\033[0m\n'
  # The one that cost hours: without it Traefik refuses the chart's IngressRoutes (their Services live in
  # another namespace), the routers never register, and requests fall through to whatever else owns the host.
  if kubectl -n kube-system get deploy traefik -o jsonpath='{.spec.template.spec.containers[0].args}' 2>/dev/null \
      | grep -q 'allowCrossNamespace=true'; then
    ok "Traefik allows cross-namespace references"
  else
    bad "Traefik lacks --providers.kubernetescrd.allowCrossNamespace=true: the UI routes will not register"
    note "kubectl apply -f deploy/review/amol-host/traefik-gateway-config.yaml"
    note "kubectl -n kube-system rollout status deploy/traefik"
  fi

  printf '\n\033[1mChart\033[0m\n'
  helm lint "$CHART" >/dev/null 2>&1 && ok "chart lints" || bad "helm lint failed on $CHART"
  helm template "$RELEASE" "$CHART" -f "$CHART/values.yaml" -f "$CHART/values-$ENV.yaml" >/dev/null 2>&1 \
    && ok "renders with values-$ENV.yaml" || bad "chart does not render with values-$ENV.yaml"

  printf '\n'
  if [[ $DOCTOR_PROBLEMS -eq 0 ]]; then
    log "ready to deploy"
  else
    die "$DOCTOR_PROBLEMS problem(s) above"
  fi
}

mirror_unbuilt_images() {  # arguments: the components built in this run
  # Switching to a registry is all-or-nothing: `global.image.registry` prefixes EVERY component's image, so
  # a component that was only ever `k3d image import`ed has no copy under that prefix and its pod lands in
  # ImagePullBackOff - the schema hook first, which fails the whole release. The images already exist
  # locally, so mirror them instead of forcing a full rebuild. A push whose layers are already in the
  # registry is a fast no-op, which is why this runs unconditionally rather than probing first.
  [[ -n "$REGISTRY" ]] || return 0
  local built=("$@") component image tag
  for component in "${COMPONENTS[@]}"; do
    contains "$component" "${built[@]}" && continue
    tag="$(running_tag "$component")"
    [[ -n "$tag" ]] || continue                     # never deployed: collect_image_tag_args reports it
    image="$(image_name "$component")"
    docker image inspect "$REGISTRY/$image:$tag" >/dev/null 2>&1 && continue
    if ! docker image inspect "$image:$tag" >/dev/null 2>&1; then
      die "$component runs tag $tag, but neither $REGISTRY/$image:$tag nor a local $image:$tag exists.
  Rebuild it so it can be pushed:  ./deploy.sh $component"
    fi
    log "mirroring $image:$tag into the registry"
    docker tag "$image:$tag" "$REGISTRY/$image:$tag"
    docker push "$REGISTRY/$image:$tag" >/dev/null
  done
}

resolve_registry() {  # decide once, and say which path the build is taking
  if [[ "$REGISTRY" == "none" ]]; then
    log "REGISTRY=none: importing images into k3d"
    REGISTRY="" REGISTRY_PULL=""
    return
  fi
  if use_local_registry; then
    log "local registry $REGISTRY (cluster pulls $REGISTRY_PULL)"
  elif [[ -z "$REGISTRY" ]]; then
    log "no usable registry: importing images into k3d (scripts/local_registry.sh sets one up)"
  else
    log "registry $REGISTRY_PULL"
  fi
}

# ------------------------------------------------------------------------------------------------ build
build_image() {  # build one component; dev: import into k3d, prod: push to REGISTRY
  local component="$1" image dir
  image="$(image_name "$component"):$TAG"
  dir="$(service_dir "$component")"
  log "building $image"
  if [[ "$component" == "ats-ui" || "$component" == "admin-ui" || "$component" == "agent-sandbox" ]]; then
    docker build -t "$image" "$ROOT/$dir"                         # self-contained build context
  else
    docker build -t "$image" -f "$ROOT/$dir/Dockerfile" "$ROOT"   # build context = repo root (needs services/common)
  fi
  if [[ -n "$REGISTRY" ]]; then
    # A registry is far faster than `k3d image import`, which ships the whole image into every node on
    # every build. Push once, the nodes pull what changed.
    docker tag "$image" "$REGISTRY/$image"
    docker push "$REGISTRY/$image"
  else
    k3d image import "$image" -c "$CLUSTER_NAME"
  fi
}

# ------------------------------------------------------------------------------------------------ KEDA
keda_installed() { kubectl get crd scaledobjects.keda.sh >/dev/null 2>&1; }
keda_installed_by_helm() { helm status keda -n "$INFRA_NAMESPACE" >/dev/null 2>&1; }

install_keda() {  # cluster-wide operator (watches all namespaces), pinned chart version, in the infra namespace
  if keda_installed && ! keda_installed_by_helm; then
    die "KEDA was installed from manifests, not Helm. Remove it first:
  kubectl delete -f https://github.com/kedacore/keda/releases/download/v$KEDA_CHART_VERSION/keda-$KEDA_CHART_VERSION.yaml"
  fi
  log "installing KEDA operator $KEDA_CHART_VERSION (namespace $INFRA_NAMESPACE)"
  helm upgrade --install keda keda --repo https://kedacore.github.io/charts --version "$KEDA_CHART_VERSION" \
    -n "$INFRA_NAMESPACE" --create-namespace --wait --timeout 10m
}

ensure_keda() {  # the dev and prod values enable the ScaledObject, which needs the operator's CRDs
  if keda_installed; then log "KEDA operator present"; else install_keda; fi
}

# ------------------------------------------------------------------------------------------------ deploy
running_tag() {  # tag the release runs now for a component (empty when never deployed)
  kubectl -n "$APP_NAMESPACE" get configmap image-tags -o jsonpath="{.data.$(tag_key "$1")}" 2>/dev/null || true
}

# Helm arguments are collected straight into this array. They used to be printed as lines and split back
# apart with `mapfile`, which is fragile twice over: an empty result becomes one empty-string argument, and
# anything else the function writes to stdout becomes an argument too. Building the array in place also
# means a `die` below exits the script rather than only a command-substitution subshell.
HELM_ARGS=()

collect_image_tag_args() {  # --set <component tag>=... per component: the new tag if built now, else the running one
  local built=("$@") component tag
  for component in "${COMPONENTS[@]}"; do
    if contains "$component" "${built[@]}"; then
      tag="$TAG"
    else
      tag="$(running_tag "$component")"
      [[ -n "$tag" ]] || die "$component has no image deployed yet, so there is no tag to keep.
  Build and deploy everything once:  ./deploy.sh
  After that, one service at a time:  ./deploy.sh $component
  Or deploy chart changes only:       ./deploy.sh apply"
    fi
    HELM_ARGS+=(--set "$(tag_value_path "$component")=$tag")
  done
}

namespace_owned_by_release() {
  local owner
  owner="$(kubectl get namespace "$1" -o jsonpath='{.metadata.annotations.meta\.helm\.sh/release-name}' 2>/dev/null || true)"
  [[ "$owner" == "$RELEASE" ]]
}

collect_ownership_args() {  # the chart owns its objects; reclaim fields that drifted away from it
  # --force-conflicts on every deploy, not just the first. These are the chart's own objects, so a field
  # changed by `kubectl edit` is drift, and a deploy is exactly when it should go back to what the chart
  # says. Without it the whole release fails on one hand-edited field and nothing - including the routing -
  # gets updated. Deliberate changes belong in values.yaml, which is also the only way they survive.
  HELM_ARGS+=(--force-conflicts)
  if namespace_owned_by_release "$APP_NAMESPACE" && namespace_owned_by_release "$INFRA_NAMESPACE"; then return; fi
  log "first Helm install: adopting the existing namespaces (--take-ownership)"
  HELM_ARGS+=(--take-ownership)
}

helm_deploy() {  # arguments: the components built in this run
  HELM_ARGS=()
  collect_image_tag_args "$@"
  collect_ownership_args
  # The cluster pulls from REGISTRY_PULL when a registry is in use; with k3d image import there is no
  # registry prefix at all and the chart's own default (no prefix) is right.
  [[ -z "$REGISTRY_PULL" ]] || HELM_ARGS+=(--set "global.image.registry=$REGISTRY_PULL")
  log "helm upgrade --install $RELEASE (values-$ENV.yaml, ${#HELM_ARGS[@]} extra arguments)"
  helm upgrade --install "$RELEASE" "$CHART" -n "$APP_NAMESPACE" --create-namespace \
    -f "$CHART/values.yaml" -f "$CHART/values-$ENV.yaml" \
    "${HELM_ARGS[@]}" --wait --timeout 15m
  log "done. UI: http://$(routing_host)/ats   Admin: http://$(routing_host)/agent-factory"
}

routing_host() {  # the host the chart routes on, so the final message is never stale
  helm get values "$RELEASE" -n "$APP_NAMESPACE" --all -o json 2>/dev/null \
    | grep -o '"host":"[^"]*"' | head -1 | cut -d'"' -f4 \
    || true
}

# ------------------------------------------------------------------------------------------------ main
reject_unknown_targets() {
  local target
  for target in "$@"; do
    contains "$target" "${COMPONENTS[@]}" && continue
    case "$target" in
      orchestrator|agent|compute|sandbox|knowledge|knowledge-worker|agent-ats-orchestrator|frontend)
        die "'$target' was renamed: use one of ${COMPONENTS[*]}" ;;
      *) die "unknown target '$target' (use: init apply keda migrate-to-helm ${COMPONENTS[*]})" ;;
    esac
  done
}

main() {
  [[ -f "$CHART/values-$ENV.yaml" ]] || die "no values file for ENV=$ENV: $CHART/values-$ENV.yaml"
  # Only dev may fall back to `k3d image import`; anything else has no local cluster to import into.
  [[ "$ENV" == "dev" || -n "$REGISTRY" ]] || die "ENV=$ENV pushes images: set REGISTRY (e.g. registry.example.com/ats)"
  require_tools kubectl helm

  case "${1:-}" in
    doctor) doctor; return ;;
    init)
      init_namespaces
      # Build infrastructure, not platform infrastructure: the registry holds the images the chart deploys,
      # so it cannot live in the chart. It is set up here instead, where a script can do the things a Helm
      # hook cannot - join a docker network and write containerd's registries.yaml on each node.
      [[ "$ENV" != "dev" ]] || bash "$ROOT/scripts/local_registry.sh" >/dev/null 2>&1 ||
        log "local registry not set up (optional): ./scripts/local_registry.sh"
      if [[ "${2:-}" == "--generate-secrets" ]]; then generate_missing_secrets; fi
      print_secret_instructions
      return ;;
    keda) install_keda; return ;;
    migrate-to-helm) shift; exec "$ROOT/scripts/cutover_to_helm.sh" "$@" ;;
  esac

  local components=()
  if [[ $# -eq 0 ]]; then
    components=("${COMPONENTS[@]}")
  elif [[ "$1" == "apply" && $# -eq 1 ]]; then
    components=()
  else
    reject_unknown_targets "$@"
    components=("$@")
  fi

  refuse_old_kustomize_objects
  require_secrets
  resolve_registry
  if [[ ${#components[@]} -gt 0 ]]; then
    require_tools docker
    # k3d is only needed for the import fallback; with a registry, docker push is all it takes.
    [[ -n "$REGISTRY" ]] || require_tools k3d
    local component
    for component in "${components[@]}"; do build_image "$component"; done
  fi
  mirror_unbuilt_images "${components[@]}"
  ensure_keda
  helm_deploy "${components[@]}"
}

# Run main only when executed (scripts/cutover_to_helm.sh sources this file to reuse its checks).
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
