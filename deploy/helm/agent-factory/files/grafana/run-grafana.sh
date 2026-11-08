#!/bin/bash
source ./logging.sh

export GF_AUTH_ANONYMOUS_ENABLED=false
export GF_PATHS_HOME=/data/grafana
export GF_PATHS_DATA=/data/grafana/data
export GF_PATHS_PLUGINS=/data/grafana/plugins
export GF_SECURITY_ADMIN_USER="${GF_SECURITY_ADMIN_USER:-admin}"
if [ -z "${GF_SECURITY_ADMIN_PASSWORD:-}" ]; then
  echo "WARN grafana-admin-password is missing in ats-platform-secrets: admin sign-in stays locked until it is set" >&2
  GF_SECURITY_ADMIN_PASSWORD="$(head -c 32 /dev/urandom | base64)"
  export GF_SECURITY_ADMIN_PASSWORD
fi

GRAFANA_API="http://localhost:3000/grafana/api"
esc() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }
api() {  # api METHOD PATH [JSON]; admin credentials go through stdin, never on the command line
  printf 'user = "%s:%s"\n' "$(esc "$GF_SECURITY_ADMIN_USER")" "$(esc "$GF_SECURITY_ADMIN_PASSWORD")" |
    curl -s -K - -X "$1" -H 'Content-Type: application/json' ${3:+-d "$3"} "$GRAFANA_API$2"
}
ensure_viewer() {
  [ -n "${GRAFANA_VIEWER_USER:-}" ] && [ -n "${GRAFANA_VIEWER_PASSWORD:-}" ] || return 0
  until curl -sf "$GRAFANA_API/health" >/dev/null; do sleep 2; done
  local login password id
  login="$(esc "$GRAFANA_VIEWER_USER")"
  password="$(esc "$GRAFANA_VIEWER_PASSWORD")"
  if api POST /admin/users "{\"name\":\"$login\",\"login\":\"$login\",\"password\":\"$password\"}" | grep -q '"id"'; then
    echo "grafana viewer account created"
    return 0
  fi
  id="$(api GET "/users/lookup?loginOrEmail=$GRAFANA_VIEWER_USER" | sed -n 's/^{"id":\([0-9]*\).*/\1/p')"
  if [ -n "$id" ] && api PUT "/admin/users/$id/password" "{\"password\":\"$password\"}" | grep -q 'updated'; then
    echo "grafana viewer account updated"
  else
    echo "WARN grafana viewer account could not be created" >&2
  fi
}
ensure_viewer &

cd ./grafana
run_with_logging "Grafana ${GRAFANA_VERSION}" "${ENABLE_LOGS_GRAFANA:-false}" ./bin/grafana server
