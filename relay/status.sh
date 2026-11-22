#!/usr/bin/env bash
# Health check for the Feishu inbound relay. Does not print secrets.
set -u
export PATH="${HOME}/.local/bin:${PATH}"
BASE="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="${BASE}/logs"
STATE_DIR="${BASE}/state"
PID_FILE="${STATE_DIR}/supervisor.pid"
RELAY_PID_FILE="${STATE_DIR}/relay.pid"
WEBHOOK_ENV="${BASE}/webhook.env"
SPOOL="${BASE}/spool/pending.jsonl"
rc=0

echo "lark-relay status"
echo "dir: ${BASE}"

if [[ -f "${PID_FILE}" ]]; then
  spid="$(cat "${PID_FILE}" 2>/dev/null || true)"
  if [[ -n "${spid}" ]] && kill -0 "${spid}" 2>/dev/null; then
    echo "supervisor: running pid=${spid}"
  else
    echo "supervisor: pid file present but process not running (pid=${spid:-empty})"
    rc=1
  fi
else
  echo "supervisor: not running (no pid file)"
  rc=1
fi

if [[ -f "${RELAY_PID_FILE}" ]]; then
  rpid="$(cat "${RELAY_PID_FILE}" 2>/dev/null || true)"
  if [[ -n "${rpid}" ]] && kill -0 "${rpid}" 2>/dev/null; then
    echo "relay.py: running pid=${rpid}"
  else
    echo "relay.py: pid file present but process not running (pid=${rpid:-empty})"
    rc=1
  fi
else
  echo "relay.py: not running (no pid file)"
  rc=1
fi

consume_n="$(ps -eo pid,cmd | awk '/@larksuite\/cli\/bin\/lark-cli event consume im.message.receive_v1/ && !/awk/ {c++} END{print c+0}')"
echo "event consume processes: ${consume_n} (expected 1 when healthy)"
if [[ "${consume_n}" -gt 1 ]]; then
  echo "WARNING: more than one consumer is running"
  rc=1
fi
if [[ "${consume_n}" -eq 0 && -f "${RELAY_PID_FILE}" ]]; then
  echo "WARNING: relay is up but no event consume process was found"
  rc=1
fi

python3 - "${WEBHOOK_ENV}" << 'PY'
import sys
from pathlib import Path
p = Path(sys.argv[1])
if not p.is_file():
    print("webhook.env: absent (events will be spooled)")
    sys.exit(0)
url = key = ""
for raw in p.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    if line.startswith("export "):
        line = line[len("export "):].strip()
    k, _, v = line.partition("=")
    v = v.strip().strip("'").strip('"')
    if k.strip() == "LARK_BOT_WEBHOOK_URL":
        url = v
    elif k.strip() == "LARK_BOT_WEBHOOK_KEY":
        key = v
if url and key:
    print("webhook.env: present, URL and key are set (values not shown)")
else:
    print("webhook.env: present but URL and/or key are empty; events will be spooled")
PY

if [[ -f "${SPOOL}" ]]; then
  spool_n="$(grep -cve '^[[:space:]]*$' "${SPOOL}" 2>/dev/null || echo 0)"
else
  spool_n=0
fi
echo "spool depth: ${spool_n}"

if [[ -f "${LOG_DIR}/relay.jsonl" ]] && grep -q '"consumer_ready"' "${LOG_DIR}/relay.jsonl"; then
  last_ready="$(grep '"consumer_ready"' "${LOG_DIR}/relay.jsonl" | tail -1)"
  echo "last ready: ${last_ready}"
else
  echo "last ready: none yet"
  if [[ "${rc}" -eq 0 ]]; then
    rc=2
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
        print("  consumer key=%s" % (c.get("event_key") or c.get("key") or c))
'
else
  echo "lark-cli: not on PATH"
  rc=1
fi

echo "logs: ${LOG_DIR}/supervisor.log ${LOG_DIR}/relay.out ${LOG_DIR}/relay.err ${LOG_DIR}/relay.jsonl ${LOG_DIR}/consume.stderr.log"
exit "${rc}"
