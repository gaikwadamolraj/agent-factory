#!/usr/bin/env bash
# Make a local Docker registry usable by the k3d cluster, and point ./deploy.sh at it.
#
#   source scripts/local_registry.sh          # set up if needed, then export REGISTRY / REGISTRY_PULL
#   ./deploy.sh agent-skill                   # builds and pushes one image, deploys, rest untouched
#
# Why: `k3d image import` copies a whole image into every node on every build, which is most of the time a
# dev deploy takes. A registry copies only the layers that changed.
#
# The registry has two names, and that is the whole trick. On this machine docker reaches it at
# localhost:5000 (the published port). Inside the cluster there is no such port, so the nodes reach it by
# container name on the k3d network. Pushing to one name and pulling from the other is why ./deploy.sh has
# both REGISTRY and REGISTRY_PULL.
#
# Run it again whenever the cluster or the registry is recreated; every step is idempotent.
set -uo pipefail

REGISTRY_NAME="${REGISTRY_NAME:-agent-factory-registry}"
REGISTRY_PORT="${REGISTRY_PORT:-5000}"
REGISTRY_STORE="${REGISTRY_STORE:-/c/docker_images}"
CLUSTER_NAME="${CLUSTER_NAME:-my-cluster}"
K3D_NETWORK="k3d-${CLUSTER_NAME}"

_log() { printf '\033[1;34m[registry]\033[0m %s\n' "$*" >&2; }
_warn() { printf '\033[1;33m[registry]\033[0m %s\n' "$*" >&2; }

# ---------------------------------------------------------------------------------------- the registry
if ! docker ps --format '{{.Names}}' | grep -qx "$REGISTRY_NAME"; then
  if docker ps -a --format '{{.Names}}' | grep -qx "$REGISTRY_NAME"; then
    _log "starting the existing $REGISTRY_NAME container"
    docker start "$REGISTRY_NAME" >/dev/null
  else
    _log "creating $REGISTRY_NAME on port $REGISTRY_PORT (store: $REGISTRY_STORE)"
    docker run -d -p "${REGISTRY_PORT}:5000" --restart=always --name "$REGISTRY_NAME" \
      -v "${REGISTRY_STORE}:/var/lib/registry" registry:2 >/dev/null
  fi
fi

# ------------------------------------------------------------------- reachable from inside the cluster
if docker network inspect "$K3D_NETWORK" >/dev/null 2>&1; then
  if docker network inspect "$K3D_NETWORK" --format '{{range .Containers}}{{.Name}} {{end}}' \
      | grep -qw "$REGISTRY_NAME"; then
    _log "$REGISTRY_NAME is already on $K3D_NETWORK"
  else
    _log "connecting $REGISTRY_NAME to $K3D_NETWORK"
    docker network connect "$K3D_NETWORK" "$REGISTRY_NAME" 2>/dev/null \
      || _warn "could not connect it to $K3D_NETWORK; the nodes will not be able to pull"
  fi
else
  _warn "no docker network $K3D_NETWORK: is the k3d cluster '$CLUSTER_NAME' running?"
fi

# ------------------------------------------------------------------------- k3s must trust it over plain HTTP
# containerd refuses an unencrypted registry unless it is listed here, and it only reads this file at start,
# so each node is restarted once. The file is tiny and idempotent, so a re-run is a no-op after the first.
REGISTRIES_YAML="mirrors:
  \"${REGISTRY_NAME}:5000\":
    endpoint:
      - \"http://${REGISTRY_NAME}:5000\"
configs:
  \"${REGISTRY_NAME}:5000\":
    tls:
      insecure_skip_verify: true
"
restarted=0
for node in $(docker ps --format '{{.Names}}' | grep "^k3d-${CLUSTER_NAME}-" | grep -Ev 'serverlb|tools'); do
  current="$(docker exec "$node" sh -c 'cat /etc/rancher/k3s/registries.yaml 2>/dev/null' 2>/dev/null || true)"
  if [[ "$current" == "$REGISTRIES_YAML" ]]; then
    continue
  fi
  _log "configuring $node to trust ${REGISTRY_NAME}:5000"
  printf '%s' "$REGISTRIES_YAML" | docker exec -i "$node" sh -c 'mkdir -p /etc/rancher/k3s && cat > /etc/rancher/k3s/registries.yaml'
  docker restart "$node" >/dev/null
  restarted=$((restarted + 1))
done
if [[ $restarted -gt 0 ]]; then
  _log "restarted $restarted node(s); give the cluster a moment before deploying"
fi

# ------------------------------------------------------------------------------------------- the two names
export REGISTRY="localhost:${REGISTRY_PORT}"          # what docker push writes to, from this machine
export REGISTRY_PULL="${REGISTRY_NAME}:5000"          # what the cluster pulls from, inside its own network
_log "REGISTRY=$REGISTRY  REGISTRY_PULL=$REGISTRY_PULL"
_log "now: ./deploy.sh            (everything)   or   ./deploy.sh <service>   (just that one)"
