#!/usr/bin/env bash
#
# Production entrypoint for plc-platform-backend.
#
# Roles (selected by the first argument, then $SERVICE_ROLE, default "api"):
#   api         - FastAPI/uvicorn HTTP + websocket server
#   worker      - dramatiq worker consuming the Redis broker
#   healthcheck - container health probe (no-op for the worker role)
#
# The same image is used for both long-running roles. The worker role is
# normally selected with `-e SERVICE_ROLE=worker` so that the baked
# HEALTHCHECK stays correct for both roles.

set -euo pipefail

log() {
  printf '%s [entrypoint] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"
}

validate_config() {
  python - <<'PY'
from plc_platform_backend.commons.configuration import get_configuration

get_configuration().validate()
PY
}

prepare_directories() {
  # Best effort: mounted volumes may be read-only or owned by another uid, in
  # which case the operator is responsible for their permissions.
  mkdir -p "${PLC_ROOT_FOLDER:-./artifacts-storage}" || true
  mkdir -p "${PLUGINS_DIRECTORY:-./user-plugins}" || true
}

run_api() {
  exec uvicorn plc_platform_backend.main:app \
    --host "${API_HOST:-0.0.0.0}" \
    --port "${API_PORT:-8000}" \
    --workers "${API_WORKERS:-1}" \
    --proxy-headers \
    --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}" \
    --log-level "${LOG_LEVEL:-info}"
}

run_worker() {
  local delay=1
  while true; do
    local exit_code=0
    dramatiq plc_platform_backend.actors || exit_code=$?
    if [ "${exit_code}" -eq 3 ]; then
      log "Broker connection error on startup. Retrying in ${delay} second(s)..."
      sleep "${delay}"
      if [ "${delay}" -lt 30 ]; then
        delay=$(( delay * 2 > 30 ? 30 : delay * 2 ))
      fi
    else
      exit "${exit_code}"
    fi
  done
}

run_healthcheck() {
  if [ "${SERVICE_ROLE:-api}" = "worker" ]; then
    # The worker exposes no HTTP endpoint; process liveness is sufficient.
    exit 0
  fi

  python - <<'PY' || exit 1
import os
import urllib.request

base_url = "http://127.0.0.1:{}".format(os.environ.get("API_PORT", "8000"))
urllib.request.urlopen("{}/health".format(base_url), timeout=3)
PY
}

main() {
  local role="${1:-${SERVICE_ROLE:-api}}"

  case "${role}" in
    api)
      prepare_directories
      validate_config
      log "Starting API server (uid=$(id -u))..."
      run_api
      ;;
    worker)
      prepare_directories
      validate_config
      log "Starting dramatiq worker (uid=$(id -u))..."
      run_worker
      ;;
    healthcheck)
      run_healthcheck
      ;;
    *)
      exec "$@"
      ;;
  esac
}

main "$@"
