#!/usr/bin/env bash
# Stop the relay supervisor (and with it relay.py and its event consumer).
#
# TERM is enough for the current supervisor. Two cases need more, and both are
# handled here:
#   - the first released supervisor.sh answers TERM by deleting its pid file and
#     restarting the relay instead of exiting;
#   - a relay.py whose supervisor died keeps holding the lock.
# So the supervisor is also found as the parent of relay.py, it is killed when
# TERM does not release the lock, and relay.py is then told to stop. relay.py
# closes its event consumer on the way out.
set -u
BASE="$(cd "$(dirname "$0")" && pwd)"
STATE_DIR="${BASE}/state"
LOCK="${STATE_DIR}/supervisor.lock"
PID_FILE="${STATE_DIR}/supervisor.pid"
RELAY_PID_FILE="${STATE_DIR}/relay.pid"
GRACE="${LARK_RELAY_STOP_GRACE:-15}"
case "${GRACE}" in
  '' | *[!0-9]*) GRACE=15 ;;
esac

if ! command -v flock >/dev/null 2>&1; then
  echo "flock not found (install util-linux); the relay supervisor needs it" >&2
  exit 1
fi

mkdir -p "${STATE_DIR}"
lock_held() { ! flock -n "${LOCK}" true 2>/dev/null; }

# True when process $1 exists and its command line contains $2.
runs() {
  case "$1" in
    '' | *[!0-9]*) return 1 ;;
  esac
  case "$(ps -o args= -p "$1" 2>/dev/null || true)" in
    *"$2"*) return 0 ;;
  esac
  return 1
}

relay_pid() {
  local pid
  pid="$(cat "${RELAY_PID_FILE}" 2>/dev/null || true)"
  if runs "${pid}" "${BASE}/relay.py"; then echo "${pid}"; fi
}

# The supervisor: from its pid file, else the parent of relay.py, else a
# supervisor.sh started from this directory by its full path.
supervisor_pid() {
  local pid relay
  pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
  if runs "${pid}" supervisor.sh; then echo "${pid}"; return; fi
  relay="$(relay_pid)"
  if [[ -n "${relay}" ]]; then
    pid="$(ps -o ppid= -p "${relay}" 2>/dev/null | tr -d ' ')"
    if runs "${pid}" supervisor.sh; then echo "${pid}"; return; fi
  fi
  ps -eo pid=,args= | awk -v s="${BASE}/supervisor.sh" 'index($0, s) && !/awk/ {print $1; exit}'
}

# Wait up to $1 seconds for the lock to be released.
wait_free() {
  local i=0 max=$(($1 * 2))
  while lock_held; do
    i=$((i + 1))
    if [[ "${i}" -gt "${max}" ]]; then
      return 1
    fi
    sleep 0.5
  done
}

if ! lock_held; then
  echo "relay is not running"
  rm -f "${PID_FILE}" "${RELAY_PID_FILE}"
  exit 0
fi

sup=""
relay=""
attempt=0
while lock_held; do
  attempt=$((attempt + 1))
  if [[ "${attempt}" -gt 3 ]]; then
    echo "relay did not stop (supervisor pid ${sup:-unknown}, relay.py pid ${relay:-unknown}); inspect ${LOCK}" >&2
    exit 1
  fi
  sup="$(supervisor_pid)"
  if [[ -n "${sup}" ]]; then
    kill -TERM "${sup}" 2>/dev/null || true
    if wait_free "${GRACE}"; then
      break
    fi
    kill -KILL "${sup}" 2>/dev/null || true
  fi
  relay="$(relay_pid)"
  if [[ -n "${relay}" ]]; then
    kill -TERM "${relay}" 2>/dev/null || true
  fi
  if [[ -z "${sup}" && -z "${relay}" ]]; then
    sleep 3 # a supervisor between two relay runs has no relay.pid yet; look again
    continue
  fi
  wait_free 30 || true
done
rm -f "${PID_FILE}" "${RELAY_PID_FILE}"
echo "relay stopped"
exit 0
