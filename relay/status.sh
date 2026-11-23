#!/usr/bin/env bash
# Health check for the Feishu inbound relay. Read-only; does not print secrets.
# Exit 0 when healthy, 1 otherwise; the last lines name each problem.
set -u
BASE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
. "${BASE}/load-env.sh"
LOG_DIR="${BASE}/logs"
STATE_DIR="${BASE}/state"
PID_FILE="${STATE_DIR}/supervisor.pid"
RELAY_PID_FILE="${STATE_DIR}/relay.pid"
WEBHOOK_ENV="${BASE}/webhook.env"
SPOOL="${BASE}/spool/pending.jsonl"

problems=""
add_problem() {
  problems="${problems}problem: $1 - $2
"
}

sup_up=0
relay_up=0
rpid=""
consume_n=0
spool_n=0

echo "lark-relay status"
echo "dir: ${BASE}"

STORE="${LARKSUITE_CLI_CONFIG_DIR:-${HOME}/.lark-cli}"
echo "lark-cli store: ${STORE}"
if [[ -f "${LARK_BOT_ENV}" ]]; then
  echo "lark-cli env file: ${LARK_BOT_ENV}"
else
  echo "lark-cli env file: ${LARK_BOT_ENV} (missing)"
fi

if ! command -v lark-cli >/dev/null 2>&1; then
  add_problem lark_cli_missing "reinstall it: npm install -g @larksuite/cli"
fi

# Without the bot's own open_id the relay cannot tell that a group message @mentions it.
if [[ -n "${LARK_BOT_OPEN_ID:-}" ]]; then
  echo "bot open_id: set"
else
  echo "bot open_id: not set"
  add_problem bot_open_id_missing "group @mentions are not forwarded; run python3 scripts/onboard.py relay --start from the skill repository, or set LARK_BOT_OPEN_ID in ${BASE}/relay.env and restart"
fi

if [[ -f "${PID_FILE}" ]]; then
  spid="$(cat "${PID_FILE}" 2>/dev/null || true)"
  if [[ -n "${spid}" ]] && kill -0 "${spid}" 2>/dev/null; then
    echo "supervisor: running pid=${spid}"
    sup_up=1
  else
    echo "supervisor: pid file present but process not running (pid=${spid:-empty})"
  fi
else
  echo "supervisor: not running (no pid file)"
fi

if [[ -f "${RELAY_PID_FILE}" ]]; then
  rpid="$(cat "${RELAY_PID_FILE}" 2>/dev/null || true)"
  if [[ -n "${rpid}" ]] && kill -0 "${rpid}" 2>/dev/null; then
    echo "relay.py: running pid=${rpid}"
    relay_up=1
  else
    echo "relay.py: pid file present but process not running (pid=${rpid:-empty})"
  fi
else
  echo "relay.py: not running (no pid file)"
fi

# Only direct children of this relay's relay.py count, so a second bot on the
# same machine does not interfere.
if [[ "${relay_up}" -eq 1 ]]; then
  consume_n="$(ps -eo ppid=,args= | awk -v p="${rpid}" '$1 == p && /event consume im\.message\.receive_v1/ {c++} END {print c+0}')"
fi
echo "event consume processes: ${consume_n} (expected 1)"

if [[ "${sup_up}" -eq 0 ]]; then
  add_problem listener_down "run ${BASE}/start.sh"
elif [[ "${relay_up}" -eq 0 || "${consume_n}" -eq 0 ]]; then
  add_problem relay_crashing "the supervisor is up but relay.py or its event consumer is not; read ${LOG_DIR}/relay.err and ${LOG_DIR}/consume.stderr.log"
fi
if [[ "${consume_n}" -gt 1 ]]; then
  add_problem duplicate_consumer "more than one event consumer under this relay; run ${BASE}/stop.sh then ${BASE}/start.sh"
fi

webhook_report() {
  python3 - "${WEBHOOK_ENV}" << 'PY'
import os
import re
import sys
from pathlib import Path

p = Path(sys.argv[1])
file_vals = {}
if p.is_file():
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        text = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        k, _, v = line.partition("=")
        file_vals[k.strip()] = v.strip().strip("'").strip('"')


def clean_key(v):
    v = v.strip()
    return re.sub(r"^(authorization:\s*)?bearer\s+", "", v, flags=re.I).strip()


sources = set()
ok = True
for name in ("LARK_BOT_WEBHOOK_URL", "LARK_BOT_WEBHOOK_KEY"):
    v = file_vals.get(name, "").strip()
    src = "webhook.env"
    if name.endswith("KEY"):
        v = clean_key(v)
    if not v:
        v = os.environ.get(name, "").strip()
        if name.endswith("KEY"):
            v = clean_key(v)
        src = "environment"
    if v:
        sources.add(src)
    else:
        ok = False
if ok:
    print("webhook: configured (%s)" % " + ".join(sorted(sources, reverse=True)))
else:
    print("webhook: not configured (events are spooled until it is)")
PY
}
webhook_line="$(webhook_report)"
echo "${webhook_line}"
case "${webhook_line}" in
  "webhook: configured"*) ;;
  *) add_problem webhook_unconfigured "save the handler routine's webhook URL and key (python3 scripts/onboard.py webhook)" ;;
esac

if [[ -f "${SPOOL}" ]]; then
  spool_n="$(grep -cve '^[[:space:]]*$' "${SPOOL}" 2>/dev/null)" || true
  spool_n="${spool_n:-0}"
fi
echo "spool depth: ${spool_n}"
if [[ "${spool_n}" -gt 0 ]] && python3 -c 'import os, sys, time; sys.exit(0 if time.time() - os.path.getmtime(sys.argv[1]) > 60 else 1)' "${SPOOL}" 2>/dev/null; then
  add_problem forwarding_stalled "${spool_n} message(s) have waited more than a minute; check the webhook URL/key and webhook_post_failed entries in ${LOG_DIR}/relay.jsonl"
fi

if [[ -f "${LOG_DIR}/relay.jsonl" ]] && grep -q '"consumer_ready"' "${LOG_DIR}/relay.jsonl"; then
  last_ready="$(grep '"consumer_ready"' "${LOG_DIR}/relay.jsonl" | tail -1)"
  echo "last ready: ${last_ready}"
else
  echo "last ready: none yet"
  if [[ "${sup_up}" -eq 1 && "${relay_up}" -eq 1 && "${consume_n}" -ge 1 ]]; then
    add_problem never_ready "the long connection has never come up; read ${LOG_DIR}/consume.stderr.log"
  fi
fi

if command -v lark-cli >/dev/null 2>&1; then
  echo "lark-cli event status --current:"
  lark-cli event status --current --json 2>/dev/null | python3 -c '
import sys, json
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except Exception:
    print("  (unparseable)")
    sys.exit(0)
apps = d.get("apps") or []
for a in apps:
    print("  app_id=%s running=%s status=%s" % (a.get("app_id"), a.get("running"), a.get("status")))
    for c in a.get("consumers") or []:
        if not isinstance(c, dict):
            print("  consumer key=%s" % (c,))
            continue
        print("  consumer key=%s received=%s dropped=%s" % (
            c.get("event_key") or c.get("key"), c.get("received", 0), c.get("dropped", 0)))
'
fi

echo "logs: ${LOG_DIR}/supervisor.log ${LOG_DIR}/relay.out ${LOG_DIR}/relay.err ${LOG_DIR}/relay.jsonl ${LOG_DIR}/consume.stderr.log"

if [[ -z "${problems}" ]]; then
  echo "status: healthy"
  exit 0
fi
echo "status: unhealthy"
printf '%s' "${problems}"
exit 1
