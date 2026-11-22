#!/usr/bin/env bash
# Restart loop for the Feishu inbound relay. Holds an exclusive lock so only
# one consumer runs. relay.py keeps lark-cli event consume's stdin open
# (required for unbounded consume; do not attach consume itself to /dev/null).
set -eu
BASE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
. "${BASE}/load-env.sh"
LOG_DIR="${BASE}/logs"
STATE_DIR="${BASE}/state"
mkdir -p "${LOG_DIR}" "${STATE_DIR}"
chmod 700 "${BASE}" "${LOG_DIR}" "${STATE_DIR}" 2>/dev/null || true

if ! command -v flock >/dev/null 2>&1; then
  echo "flock not found (install util-linux); the relay supervisor needs it" >&2
  exit 1
fi

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

RELAY_PID=""
SLEEP_PID=""
cleanup() {
  if [[ -n "${SLEEP_PID}" ]]; then
    kill "${SLEEP_PID}" 2>/dev/null || true
  fi
  if [[ -n "${RELAY_PID}" ]] && kill -0 "${RELAY_PID}" 2>/dev/null; then
    kill -TERM "${RELAY_PID}" 2>/dev/null || true
    wait "${RELAY_PID}" 2>/dev/null || true
  fi
  rm -f "${PID_FILE}" "${RELAY_PID_FILE}"
}
# TERM and INT must end the supervisor (cleanup runs via the EXIT trap), not
# just run cleanup and fall back into the restart loop.
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) supervisor start pid=$$" >> "${LOG_DIR}/supervisor.log"

# Restart backoff: 2 s, doubling up to 60 s while the relay keeps dying within
# 30 s of starting; a run of 30 s or more resets it.
delay=2
while true; do
  started="$(date +%s)"
  python3 "${BASE}/relay.py" >> "${LOG_DIR}/relay.out" 2>> "${LOG_DIR}/relay.err" &
  RELAY_PID=$!
  echo "${RELAY_PID}" > "${RELAY_PID_FILE}"
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) relay start pid=${RELAY_PID}" >> "${LOG_DIR}/supervisor.log"
  set +e
  wait "${RELAY_PID}"
  rc=$?
  set -e
  ran=$(( $(date +%s) - started ))
  if [[ "${ran}" -ge 30 ]]; then
    delay=2
  fi
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) relay exit pid=${RELAY_PID} rc=${rc} ran=${ran}s delay=${delay}s" >> "${LOG_DIR}/supervisor.log"
  rm -f "${RELAY_PID_FILE}"
  RELAY_PID=""
  # Interruptible sleep so TERM stops the supervisor promptly. The sleep does
  # not inherit the lock fd.
  sleep "${delay}" 9>&- &
  SLEEP_PID=$!
  wait "${SLEEP_PID}" 2>/dev/null || true
  SLEEP_PID=""
  if [[ "${ran}" -lt 30 ]]; then
    delay=$(( delay * 2 ))
    if [[ "${delay}" -gt 60 ]]; then
      delay=60
    fi
  fi
done
