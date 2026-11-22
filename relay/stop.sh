#!/usr/bin/env bash
# Stop the relay supervisor (and with it relay.py and its event consumer).
set -u
BASE="$(cd "$(dirname "$0")" && pwd)"
STATE_DIR="${BASE}/state"
LOCK="${STATE_DIR}/supervisor.lock"
PID_FILE="${STATE_DIR}/supervisor.pid"
RELAY_PID_FILE="${STATE_DIR}/relay.pid"

if ! command -v flock >/dev/null 2>&1; then
  echo "flock not found (install util-linux); the relay supervisor needs it" >&2
  exit 1
fi

mkdir -p "${STATE_DIR}"
lock_held() { ! flock -n "${LOCK}" true 2>/dev/null; }

if ! lock_held; then
  echo "relay is not running"
  rm -f "${PID_FILE}" "${RELAY_PID_FILE}"
  exit 0
fi

pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
args=""
if [[ -n "${pid}" ]]; then
  args="$(ps -o args= -p "${pid}" 2>/dev/null || true)"
fi
case "${args}" in
  *supervisor.sh*) ;;
  *)
    echo "cannot identify the supervisor process (lock is held); inspect ${BASE}/state/supervisor.pid" >&2
    exit 1
    ;;
esac

kill -TERM "${pid}" 2>/dev/null || true
i=0
while lock_held; do
  i=$((i + 1))
  if [[ "${i}" -gt 60 ]]; then
    echo "relay did not stop within 30s (pid ${pid})" >&2
    exit 1
  fi
  sleep 0.5
done
echo "relay stopped"
exit 0
