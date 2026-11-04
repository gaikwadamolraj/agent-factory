#!/usr/bin/env bash
# Build, import into k3d and deploy the ATS platform with kustomize (namespace ats-platform).
#
#   ./deploy.sh                          # everything: schema job -> observability + all services
#   ./deploy.sh schema                   # only (re)run the schema migration Job
#   ./deploy.sh agent orchestrator       # rebuild + roll out selected services (others keep their current image)
#   ENV=prod ./deploy.sh ...             # use deploy/k8s/overlays/prod (expects registry images, no k3d import)
#
# Env: CLUSTER_NAME (my-cluster), ENV (dev), IMAGE_TAG (default: timestamp).
# Requires the Secret ats-platform-secrets in namespace ats-platform (see deploy/k8s/README.md).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLUSTER_NAME="${CLUSTER_NAME:-my-cluster}"
ENV="${ENV:-dev}"
NAMESPACE="ats-platform"
OVERLAY="$ROOT/deploy/k8s/overlays/$ENV"
TAG="${IMAGE_TAG:-$(date +%Y%m%d-%H%M%S)}"
SERVICES=(sandbox compute agent orchestrator frontend)
ALL=(schema "${SERVICES[@]}")

log() { printf '\033[1;34m[deploy]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[deploy] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }
contains() { local x; for x in "${@:2}"; do [[ "$x" == "$1" ]] && return 0; done; return 1; }

targets=("$@")
[[ ${#targets[@]} -eq 0 ]] && targets=("${ALL[@]}")
for t in "${targets[@]}"; do contains "$t" "${ALL[@]}" || die "unknown target '$t' (use: ${ALL[*]})"; done
for tool in docker kubectl; do command -v "$tool" >/dev/null || die "'$tool' is not installed"; done
[[ -d "$OVERLAY" ]] || die "overlay not found: $OVERLAY"
kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"
kubectl -n "$NAMESPACE" get secret ats-platform-secrets >/dev/null 2>&1 || die "Secret ats-platform-secrets is missing in $NAMESPACE"

build() {  # build + import one image
  local svc="$1" image="ats-$1:$TAG"
  log "building $image"
  if [[ "$svc" == "frontend" || "$svc" == "sandbox" ]]; then   # self-contained build contexts
    docker build -t "$image" "$ROOT/services/$svc"
  else
    docker build -t "$image" -f "$ROOT/services/$svc/Dockerfile" "$ROOT"
  fi
  if [[ "$ENV" == "dev" ]]; then
    command -v k3d >/dev/null || die "'k3d' is not installed"
    k3d image import "$image" -c "$CLUSTER_NAME"
  fi
}

current_image() {  # image currently deployed for a service (empty when not deployed yet)
  kubectl -n "$NAMESPACE" get deployment "ats-$1" -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true
}

for t in "${targets[@]}"; do build "$t"; done

# ---------------------------------------------------------------- schema job (runs before the services)
if contains schema "${targets[@]}"; then
  log "running schema migration job"
  kubectl -n "$NAMESPACE" delete job ats-schema-migrate --ignore-not-found --wait=true
  kubectl kustomize "$OVERLAY/schema" | sed "s#image: ats-schema:dev#image: ats-schema:$TAG#" | kubectl apply -f -
  if ! kubectl -n "$NAMESPACE" wait --for=condition=complete job/ats-schema-migrate --timeout=300s; then
    kubectl -n "$NAMESPACE" logs job/ats-schema-migrate --tail=50 || true
    die "schema job failed"
  fi
  kubectl -n "$NAMESPACE" logs job/ats-schema-migrate --tail=20
fi

# ---------------------------------------------------------------- services
if [[ ${#targets[@]} -gt 1 || "${targets[0]}" != "schema" ]]; then
  substitutions=()
  for svc in "${SERVICES[@]}"; do
    if contains "$svc" "${targets[@]}"; then
      substitutions+=(-e "s#image: ats-$svc:dev#image: ats-$svc:$TAG#")
    else
      existing="$(current_image "$svc")"
      [[ -n "$existing" ]] || die "ats-$svc is not deployed yet; include it in the targets"
      substitutions+=(-e "s#image: ats-$svc:dev#image: $existing#")
    fi
  done
  log "applying $OVERLAY"
  kubectl kustomize "$OVERLAY" | sed "${substitutions[@]}" | kubectl apply -f -
  for deployment in ats-lgtm ats-sandbox ats-compute ats-agent ats-orchestrator ats-frontend; do
    kubectl -n "$NAMESPACE" rollout status "deployment/$deployment" --timeout=10m
  done
fi
log "done (tag $TAG). UI: http://amol.rackspire.ai/ats  Grafana: http://amol.rackspire.ai/grafana"
