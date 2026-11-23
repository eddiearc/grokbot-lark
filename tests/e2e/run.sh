#!/usr/bin/env bash
# End-to-end rehearsal of the whole skill against a fake lark-cli and a local webhook.
#
#   bash tests/e2e/run.sh
#
# Linux only (needs flock, setsid, ps). It never touches the real home directory: HOME is
# pointed at a fresh temp directory, which is kept afterwards for inspection.
#
# Covers: onboard.py app -> wait -> scopes -> relay -> webhook, start.sh, an inbound message
# reaching the webhook, status.sh, reaction removal after the bot's reply, stop.sh.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "${HERE}/../.." && pwd)"

for tool in flock setsid ps python3; do
  if ! command -v "${tool}" >/dev/null 2>&1; then
    echo "SKIP: ${tool} not found (this rehearsal needs Linux with util-linux and procps)"
    exit 0
  fi
done

WORK="$(mktemp -d "${TMPDIR:-/tmp}/grokbot-lark-e2e.XXXXXX")"
export HOME="${WORK}/home"
export FAKE_CTRL="${WORK}/ctrl"
export FAKE_SCOPES="${REPO}/scopes/bot-scopes.txt"
mkdir -p "${HOME}/.local/bin" "${FAKE_CTRL}"
cp "${HERE}/fake-lark-cli.py" "${HOME}/.local/bin/lark-cli"
chmod +x "${HOME}/.local/bin/lark-cli"
export PATH="${HOME}/.local/bin:${PATH}"
RELAY="${HOME}/.lark-relay-e2e"   # a bot other than the default gets a relay directory of its own
HOOK_PID=""
fail=0

cleanup() {
  [[ -x "${RELAY}/stop.sh" ]] && "${RELAY}/stop.sh" >/dev/null 2>&1
  [[ -n "${HOOK_PID}" ]] && kill "${HOOK_PID}" 2>/dev/null
  echo "work dir kept: ${WORK}"
}
trap cleanup EXIT

check() {
  if eval "$2"; then echo "  PASS  $1"; else echo "  FAIL  $1"; fail=1; fi
}
cd "${REPO}"

echo "== 1. app: link and QR image"
python3 scripts/onboard.py app --bot e2e --name "Atlas" > "${WORK}/app.out" 2>&1
rc=$?
check "app exits 0 and waits for a scan" '[[ ${rc} -eq 0 ]] && grep -q "^status: waiting_for_scan" "${WORK}/app.out"'
check "link carries name, scopes and createOnly" 'grep -q "^verification_url: .*&name=Atlas&.*&addons=.*&createOnly=true$" "${WORK}/app.out"'
check "QR file exists" 'test -f "$(sed -n "s/^qr_png: //p" "${WORK}/app.out")"'

echo "== 2. wait: returns by itself when the operator finishes"
( sleep 2; touch "${FAKE_CTRL}/scan-done" ) &
python3 scripts/onboard.py wait --bot e2e --timeout 30 > "${WORK}/wait.out" 2>&1
rc=$?
check "wait reports ready with the bot open_id" '[[ ${rc} -eq 0 ]] && grep -q "^status: ready" "${WORK}/wait.out" && grep -q "^bot_open_id: ou_bot" "${WORK}/wait.out"'

echo "== 3. scopes"
python3 scripts/onboard.py scopes --bot e2e > "${WORK}/scopes.out" 2>&1
rc=$?
check "scopes complete and bridge ready" '[[ ${rc} -eq 0 ]] && grep -q "^status: complete" "${WORK}/scopes.out" && grep -q "^bridge_ready: yes" "${WORK}/scopes.out"'

echo "== 4. relay, webhook, start"
python3 "${HERE}/webhook-server.py" "${WORK}/hook.port" "${WORK}/hook.log" &
HOOK_PID=$!
for _ in $(seq 1 40); do [[ -s "${WORK}/hook.port" ]] && break; sleep 0.1; done
python3 scripts/onboard.py relay --bot e2e > "${WORK}/relay.out" 2>&1
rc=$?
check "relay installed, webhook not configured yet" '[[ ${rc} -eq 0 ]] && grep -q "^status: installed" "${WORK}/relay.out" && grep -q "^webhook: not configured" "${WORK}/relay.out"'
check "relay.env carries the bot identity" 'grep -q "^LARK_BOT_OPEN_ID=ou_bot" "${RELAY}/relay.env" && grep -q "^LARK_BOT_ENV=${HOME}/.lark-bots/e2e/env.sh" "${RELAY}/relay.env"'
LARK_BOT_WEBHOOK_KEY="Bearer test-key-123" ONBOARD_SKIP_WEBHOOK_TEST=0 python3 scripts/onboard.py webhook --bot e2e --url "http://127.0.0.1:$(cat "${WORK}/hook.port")/hook" > "${WORK}/webhook.out" 2>&1
rc=$?
check "webhook saved, connectivity check accepted, key not printed" '[[ ${rc} -eq 0 ]] && grep -q "^status: saved" "${WORK}/webhook.out" && grep -q "^test: accepted (HTTP 200)" "${WORK}/webhook.out" && ! grep -q "test-key-123" "${WORK}/webhook.out"'
check "webhook.env is private and the Bearer prefix is gone" '[[ "$(stat -c %a "${RELAY}/webhook.env")" == "600" ]] && grep -q "^LARK_BOT_WEBHOOK_KEY=test-key-123$" "${RELAY}/webhook.env"'
python3 scripts/onboard.py routine health-check --bot e2e > "${WORK}/routine.out" 2>&1
check "routine health-check names this bot's relay directory" 'grep -q "~/.lark-relay-e2e/status.sh" "${WORK}/routine.out"'
"${RELAY}/start.sh" > "${WORK}/start.out" 2>&1
rc=$?
check "start.sh reports the long connection ready" '[[ ${rc} -eq 0 ]] && grep -q "long connection ready" "${WORK}/start.out"'
"${RELAY}/start.sh" > "${WORK}/start2.out" 2>&1
check "a second start.sh says already running" 'grep -q "already running" "${WORK}/start2.out"'

echo "== 5. an inbound message reaches the webhook"
# The first request in the log is setup's relay_test; wait for the forwarded message itself.
for _ in $(seq 1 40); do grep -q '"message_id"' "${WORK}/hook.log" 2>/dev/null && break; sleep 0.5; done
python3 - "${WORK}/hook.log" "${HOME}" << 'PY'
import json
import sys

records = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
rec = next(r for r in records if "message_id" in r["body"])
body = rec["body"]
ok = True


def check(name, cond):
    global ok
    print(("  PASS  " if cond else "  FAIL  ") + name)
    ok = ok and bool(cond)


check("setup sent a relay_test request first", any(r["body"].get("type") == "relay_test" for r in records))
check("bearer key sent", rec["authorization"] == "Bearer test-key-123")
check("message id and text", body["message_id"] == "om_1" and body["text"] == "hello bot")
check("sender resolved", body["sender"].get("name") == "Alice" and body["sender"].get("email") == "alice@example.com")
check("reaction id recorded", body.get("reaction_id") == "r_1")
check("console_url points at this app", body.get("console_url") == "https://open.feishu.cn/app/cli_fake/auth")
check("instructions source this bot's store", 'source "%s/.lark-bots/e2e/env.sh"' % sys.argv[2] in body["instructions"])
sys.exit(0 if ok else 1)
PY
[[ $? -eq 0 ]] || fail=1

echo "== 6. status while healthy"
"${RELAY}/status.sh" > "${WORK}/status.out" 2>&1
rc=$?
check "status healthy" '[[ ${rc} -eq 0 ]] && grep -q "^status: healthy" "${WORK}/status.out" && grep -q "event consume processes: 1" "${WORK}/status.out"'
check "every relay call used this bot's store" 'grep -q "\"config_dir\": \"${HOME}/.lark-bots/e2e/config\"" "${FAKE_CTRL}/calls.log" && ! grep -q "/.lark-cli\"" "${FAKE_CTRL}/calls.log"'

echo "== 7. the bot replies; the working reaction goes away at the next poll (up to 25 s)"
touch "${FAKE_CTRL}/replied"
for _ in $(seq 1 60); do [[ -f "${FAKE_CTRL}/reaction-deleted" ]] && break; sleep 0.5; done
check "reaction removed after the reply" 'test -f "${FAKE_CTRL}/reaction-deleted" && grep -q "reaction_complete" "${RELAY}/logs/relay.jsonl"'

echo "== 8. stop"
"${RELAY}/stop.sh" > "${WORK}/stop.out" 2>&1
rc=$?
check "stop.sh reports stopped" '[[ ${rc} -eq 0 ]] && grep -q "^relay stopped" "${WORK}/stop.out"'
sleep 1
check "no relay or consumer is left" '! pgrep -f "${RELAY}/relay.py" >/dev/null && ! pgrep -f "lark-cli event consume" >/dev/null'
"${RELAY}/status.sh" > "${WORK}/status2.out" 2>&1
rc=$?
check "status names the fix after stop" '[[ ${rc} -eq 1 ]] && grep -q "^problem: listener_down - run ${RELAY}/start.sh" "${WORK}/status2.out"'

echo
if [[ ${fail} -eq 0 ]]; then echo "E2E RESULT: ALL PASS"; else echo "E2E RESULT: FAILURES (see ${WORK})"; fi
exit ${fail}
