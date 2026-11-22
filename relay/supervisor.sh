#!/usr/bin/env bash
# Restart loop for the Feishu inbound relay. Holds an exclusive lock so only
# one consumer runs. relay.py keeps lark-cli event consume's stdin open
# (required for unbounded consume; do not attach consume itself to /dev/null).
set -eu
export PATH="${HOME}/.local/bin:${PATH}"
# Per-bot isolated lark-cli store (CONFIG_DIR/DATA_DIR/LOG_DIR). Override path
# with LARK_BOT_ENV. The file must contain only those dir vars and PATH, never
# secrets. If the file is missing, lark-cli falls back to ~/.lark-cli.
LARK_BOT_ENV="${LARK_BOT_ENV:-${HOME}/.lark-bots/lark-bot/env.sh}"
if [[ -f "${LARK_BOT_ENV}" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "${LARK_BOT_ENV}"
  set +a
fi
BASE="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="${BASE}/logs"
STATE_DIR="${BASE}/state"
mkdir -p "${LOG_DIR}" "${STATE_DIR}"
chmod 700 "${BASE}" "${LOG_DIR}" "${STATE_DIR}" 2>/dev/null || true

LOCK="${STATE_DIR}/supervisor.lock"
PID_FILE="${STATE_DIR}/supervisor.pid"
RELAY_PID_FILE="${STATE_DIR}/relay.pid"

exec 9>"${LOCK}"
if ! flock -n 9; then
  existing="$(cat "${PID_FILE}" 2>/dev/null || true)"
  echo "supervisor already running (pid ${existing:-unknown}); refusing to start a second consumer" >&2
  exit 1
fi

echo "$$" > "${PID_FILE}"
chmod 600 "${PID_FILE}" "${LOCK}" 2>/dev/null || true

cleanup() {
  if [[ -n "${RELAY_PID:-}" ]] && kill -0 "${RELAY_PID}" 2>/dev/null; then
    kill -TERM "${RELAY_PID}" 2>/dev/null || true
    wait "${RELAY_PID}" 2>/dev/null || true
  fi
  rm -f "${PID_FILE}" "${RELAY_PID_FILE}"
}
trap cleanup EXIT INT TERM

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) supervisor start pid=$$" >> "${LOG_DIR}/supervisor.log"

while true; do
  python3 "${BASE}/relay.py" >> "${LOG_DIR}/relay.out" 2>> "${LOG_DIR}/relay.err" &
  RELAY_PID=$!
  echo "${RELAY_PID}" > "${RELAY_PID_FILE}"
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) relay start pid=${RELAY_PID}" >> "${LOG_DIR}/supervisor.log"
  set +e
  wait "${RELAY_PID}"
  rc=$?
  set -e
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) relay exit pid=${RELAY_PID} rc=${rc}" >> "${LOG_DIR}/supervisor.log"
  rm -f "${RELAY_PID_FILE}"
  RELAY_PID=""
  sleep 2
done
