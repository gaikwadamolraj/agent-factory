#!/usr/bin/env bash
# One-time switch of namespace ats-platform from the old kustomize objects to the Helm release (fresh start: no data
# is kept; Postgres, Redis and FalkorDB start empty in namespace agent-infra).
#
#   ./deploy.sh migrate-to-helm --dry-run     # print every step, change nothing (read-only kubectl only)
#   ./deploy.sh migrate-to-helm               # do it
#
# Steps (each one is safe to re-run):
#   1. back up the YAML of every object kind we touch in ats-platform to backups/<timestamp>/ (Secrets: names and
#      key names only, never values)
#   2. check the required Secrets and keys (stops with the list of what is missing)
#   3. delete the old kustomize objects by name (never Secrets; the old FalkorDB PVC goes too: fresh start)
#   4. full ./deploy.sh: build every image, install KEDA if missing, helm upgrade --install (adopts the namespaces)
#   5. print the verification steps
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Reuse the namespace names, Secret checks and logging of deploy.sh (sourcing it does not run its main).
# shellcheck source=../deploy.sh
source "$ROOT/deploy.sh"

DRY_RUN=false
BACKUP_DIR="$ROOT/backups/$(date +%Y%m%d-%H%M%S)"
BACKUP_KINDS=(deployments statefulsets services configmaps ingresses ingressroutes.traefik.io middlewares.traefik.io
              networkpolicies scaledobjects.keda.sh triggerauthentications.keda.sh jobs persistentvolumeclaims)

# Old kustomize objects in ats-platform, by kind. ScaledObjects first, so KEDA stops scaling the old worker.
OLD_SCALEDOBJECTS=(ats-knowledge-worker)
OLD_TRIGGERAUTHENTICATIONS=(ats-knowledge-worker-postgres)
OLD_DEPLOYMENTS=(ats-orchestrator ats-agent ats-compute ats-sandbox ats-knowledge ats-knowledge-worker
                 ats-knowledge-redis ats-llm-gateway ats-lgtm ats-frontend)
OLD_STATEFULSETS=(ats-falkordb)
OLD_SERVICES=(ats-orchestrator ats-agent ats-compute ats-sandbox ats-knowledge ats-knowledge-redis ats-llm-gateway
              ats-lgtm ats-falkordb ats-frontend postgres-service redis-service)
OLD_CONFIGMAPS=(ats-orchestrator-config ats-agent-config ats-compute-config ats-knowledge-config ats-frontend-config
                ats-schema-config ats-grafana-settings ats-grafana-provisioning ats-grafana-start ats-grafana-dashboards)
OLD_HASHED_CONFIGMAPS='^ats-llm-(config|backends|gateway-models)-[a-z0-9]{10}$'   # kustomize configMapGenerator
OLD_NETWORKPOLICIES=(ats-falkordb-clients ats-knowledge-callers ats-knowledge-redis-clients ats-llm-gateway-callers
                     ats-sandbox-isolation)
OLD_INGRESSES=(ats-platform)
OLD_INGRESSROUTES=(ats-platform-limited)
OLD_MIDDLEWARES=(ats-security-headers ats-rate-limit ats-rate-limit-auth ats-rate-limit-turns)
OLD_JOBS=(ats-schema-migrate)
OLD_PVCS=(data-ats-falkordb-0)

run() {  # print a command; run it unless --dry-run
  printf '  $ %s\n' "$*"
  [[ "$DRY_RUN" == "true" ]] || "$@"
}

step() { log "step $1"; }

# ------------------------------------------------------------------------------------------------ 1. backup
backup_kind() {  # one YAML file per kind (kubectl output, read-only)
  local kind="$1" file="$BACKUP_DIR/${1%%.*}.yaml"
  printf '  $ kubectl -n %s get %s -o yaml > %s\n' "$APP_NAMESPACE" "$kind" "$file"
  [[ "$DRY_RUN" == "true" ]] || kubectl -n "$APP_NAMESPACE" get "$kind" -o yaml > "$file"
}

backup_secret_key_names() {  # which Secrets and keys existed, never their values
  local file="$BACKUP_DIR/secret-key-names.txt"
  printf '  $ (Secret names and key names) > %s\n' "$file"
  [[ "$DRY_RUN" == "true" ]] || kubectl -n "$APP_NAMESPACE" get secrets \
    -o go-template='{{range .items}}{{.metadata.name}}:{{range $key, $_ := .data}} {{$key}}{{end}}{{"\n"}}{{end}}' > "$file"
}

backup_objects() {
  step "1/5: back up ats-platform to ${BACKUP_DIR#"$ROOT"/}"
  [[ "$DRY_RUN" == "true" ]] || mkdir -p "$BACKUP_DIR"
  local kind
  for kind in "${BACKUP_KINDS[@]}"; do backup_kind "$kind"; done
  backup_secret_key_names
}

# ------------------------------------------------------------------------------------------------ 2. secrets
check_secrets() {  # a dry run reports problems and goes on; a real run stops
  step "2/5: check the required Secrets"
  if [[ "$DRY_RUN" == "true" ]]; then
    ( require_secrets ) && log "Secrets complete" || log "(dry run) fix the Secrets above before the real run"
  else
    require_secrets
    log "Secrets complete"
  fi
}

# ------------------------------------------------------------------------------------------------ 3. delete
delete_named() {  # kind, then names; missing objects are fine (re-runs)
  local kind="$1"
  shift
  [[ $# -gt 0 ]] || return 0
  run kubectl -n "$APP_NAMESPACE" delete "$kind" "$@" --ignore-not-found --wait=true
}

old_hashed_configmaps() {
  kubectl -n "$APP_NAMESPACE" get configmaps -o name | sed 's#^configmap/##' | grep -E "$OLD_HASHED_CONFIGMAPS" || true
}

delete_old_objects() {
  step "3/5: delete the old kustomize objects (Secrets are kept)"
  local hashed_configmaps=()
  mapfile -t hashed_configmaps < <(old_hashed_configmaps)
  delete_named scaledobjects.keda.sh "${OLD_SCALEDOBJECTS[@]}"
  delete_named triggerauthentications.keda.sh "${OLD_TRIGGERAUTHENTICATIONS[@]}"
  delete_named deployments "${OLD_DEPLOYMENTS[@]}"
  delete_named statefulsets "${OLD_STATEFULSETS[@]}"
  delete_named services "${OLD_SERVICES[@]}"
  delete_named configmaps "${OLD_CONFIGMAPS[@]}" "${hashed_configmaps[@]}"
  delete_named networkpolicies "${OLD_NETWORKPOLICIES[@]}"
  delete_named ingresses "${OLD_INGRESSES[@]}"
  delete_named ingressroutes.traefik.io "${OLD_INGRESSROUTES[@]}"
  delete_named middlewares.traefik.io "${OLD_MIDDLEWARES[@]}"
  delete_named jobs "${OLD_JOBS[@]}"
  delete_named persistentvolumeclaims "${OLD_PVCS[@]}"   # fresh start: the old graph data is not kept
}

# ------------------------------------------------------------------------------------------------ 4. install
install_release() {
  step "4/5: build every image and install the Helm release"
  run "$ROOT/deploy.sh"
}

# ------------------------------------------------------------------------------------------------ 5. verify
print_verification() {
  step "5/5: verify"
  cat <<EOF
  helm -n $APP_NAMESPACE status $RELEASE
  kubectl -n $APP_NAMESPACE get pods,svc,ingress          # agent-* services, ats-ui, aliases
  kubectl -n $INFRA_NAMESPACE get pods,svc,pvc,ingress    # postgres, redis, falkordb, llm-gateway, lgtm
  kubectl -n $APP_NAMESPACE logs job/ats-schema-migrate   # migration + demo seed (dev)
  kubectl -n $APP_NAMESPACE get scaledobject              # agent-knowledge-graph-worker READY=True
  open http://amol.rackspire.ai/ats and sign in; run one screening; ask the assistant a question
  open http://amol.rackspire.ai/grafana -> ATS Platform Overview shows ats-orchestrator traffic
Not touched: the manually created middlewares security-headers, rate-limit-2rps, file-upload-limit, zero-trust-auth
(unused, see deploy/README.md), the Secrets, and Postgres/Redis in namespace amol (delete them when this works).
Roll back: the YAML in ${BACKUP_DIR#"$ROOT"/} (old images ats-*:<tag> are still in k3d).
EOF
}

# ------------------------------------------------------------------------------------------------ main
cutover_main() {
  [[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true
  [[ "$DRY_RUN" == "true" ]] && log "DRY RUN: only read-only kubectl calls, nothing is changed"
  require_tools kubectl helm
  backup_objects
  check_secrets
  delete_old_objects
  install_release
  print_verification
}

cutover_main "$@"
