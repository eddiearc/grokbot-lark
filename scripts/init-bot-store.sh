#!/usr/bin/env bash
# Compatibility wrapper: import an EXISTING app into an isolated lark-cli store.
#
# New setups should use the QR flow instead: python3 scripts/onboard.py app
#
# Usage:
#   printf '%s' "$APP_SECRET" | scripts/init-bot-store.sh <bot-name> <app-id> [brand]
#
# Reads the app secret from stdin (never from argv or the env file). Does not
# print the secret. brand defaults to feishu.
set -euo pipefail

if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo "usage: $0 <bot-name> <app-id> [brand]" >&2
  exit 2
fi

exec python3 "$(dirname "$0")/onboard.py" app --bot "$1" --app-id "$2" --brand "${3:-feishu}"
