#!/usr/bin/env bash
# Start the relay supervisor detached from the calling shell. Safe to run twice.
set -u
BASE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
. "${BASE}/load-env.sh"
LOG_DIR="${BASE}/logs"
STATE_DIR="${BASE}/state"
mkdir -p "${LOG_DIR}" "${STATE_DIR}"
chmod 700 "${LOG_DIR}" "${STATE_DIR}" 2>/dev/null || true

if ! command -v flock >/dev/null 2>&1; then
  echo "flock not found (install util-linux); the relay supervisor needs it" >&2
  exit 1
fi

LOCK="${STATE_DIR}/supervisor.lock"
PID_FILE="${STATE_DIR}/supervisor.pid"
READY_LOG="${LOG_DIR}/relay.jsonl"

lock_held() { ! flock -n "${LOCK}" true 2>/dev/null; }
sup_pid() { cat "${PID_FILE}" 2>/dev/null || true; }
ready_count() {
  local n
  n="$(grep -c '"consumer_ready"' "${READY_LOG}" 2>/dev/null)" || true
  echo "${n:-0}"
}

if lock_held; then
  pid="$(sup_pid)"
  echo "relay already running (supervisor pid ${pid:-unknown})"
  exit 0
fi

before="$(ready_count)"
if command -v setsid >/dev/null 2>&1; then
  setsid nohup "${BASE}/supervisor.sh" >> "${LOG_DIR}/supervisor.out" 2>&1 < /dev/null &
else
  nohup "${BASE}/supervisor.sh" >> "${LOG_DIR}/supervisor.out" 2>&1 < /dev/null &
fi

i=0
while ! lock_held; do
  i=$((i + 1))
  if [[ "${i}" -gt 20 ]]; then
    echo "relay failed to start; see ${LOG_DIR}/supervisor.out and ${LOG_DIR}/supervisor.log" >&2
    exit 1
  fi
  sleep 0.5
done

timeout_s="${LARK_RELAY_READY_TIMEOUT:-60}"
case "${timeout_s}" in
  '' | *[!0-9]*) timeout_s=60 ;;
esac
i=0
max=$((timeout_s * 2))
while [[ "$(ready_count)" -le "${before}" ]]; do
  i=$((i + 1))
  if [[ "${i}" -gt "${max}" ]]; then
    echo "relay started (supervisor pid $(sup_pid)) but the long connection is not ready yet; run ${BASE}/status.sh and read ${LOG_DIR}/consume.stderr.log"
    exit 2
  fi
  sleep 0.5
done
echo "relay started (supervisor pid $(sup_pid)); long connection ready"
exit 0
