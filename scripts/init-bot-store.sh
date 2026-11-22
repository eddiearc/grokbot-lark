#!/usr/bin/env bash
# Initialize an isolated lark-cli store for one bot.
#
# Usage:
#   printf '%s' "$APP_SECRET" | scripts/init-bot-store.sh <bot-name> <app-id> [brand]
#
# Reads the app secret from stdin (never from argv or the env file). Does not
# print the secret. brand defaults to feishu. After init, writes env.sh with
# only CONFIG_DIR, DATA_DIR, LOG_DIR, and PATH, then sets strict-mode bot
# in that store.
set -euo pipefail

export PATH="${HOME}/.local/bin:${PATH}"

if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo "usage: $0 <bot-name> <app-id> [brand]" >&2
  exit 2
fi

BOT_NAME="$1"
APP_ID="$2"
BRAND="${3:-feishu}"
STORE="${HOME}/.lark-bots/${BOT_NAME}"
CONFIG="${STORE}/config"
DATA="${STORE}/data"
LOGS="${STORE}/logs"
ENV_SH="${STORE}/env.sh"

if ! command -v lark-cli >/dev/null 2>&1; then
  echo "lark-cli not found on PATH" >&2
  exit 1
fi

if [[ ! -t 0 ]]; then
  SECRET="$(cat)"
else
  echo "read the app secret from stdin (this script will not echo it)" >&2
  exit 2
fi

if [[ -z "${SECRET}" ]]; then
  echo "empty secret on stdin" >&2
  exit 2
fi

umask 077
mkdir -p "${CONFIG}" "${DATA}" "${LOGS}"
chmod 700 "${HOME}/.lark-bots" "${STORE}" "${CONFIG}" "${DATA}" "${LOGS}" 2>/dev/null || true

export LARKSUITE_CLI_CONFIG_DIR="${CONFIG}"
export LARKSUITE_CLI_DATA_DIR="${DATA}"
export LARKSUITE_CLI_LOG_DIR="${LOGS}"

# Do not export the secret. Feed it on stdin.
printf '%s' "${SECRET}" | lark-cli config init --app-id "${APP_ID}" --app-secret-stdin --brand "${BRAND}"
unset SECRET
lark-cli config strict-mode bot --global

cat > "${ENV_SH}" << ENV
# Isolated lark-cli store for ${BOT_NAME}. This file contains no secrets.
# Usage: source ${ENV_SH} && lark-cli ... --as bot
export PATH="\${HOME}/.local/bin:\${PATH}"
export LARKSUITE_CLI_CONFIG_DIR="${CONFIG}"
export LARKSUITE_CLI_DATA_DIR="${DATA}"
export LARKSUITE_CLI_LOG_DIR="${LOGS}"
ENV
chmod 600 "${ENV_SH}"

echo "initialized isolated store at ${STORE}"
echo "env file: ${ENV_SH}"
echo "next: source ${ENV_SH} && lark-cli whoami"
