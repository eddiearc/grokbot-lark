# Sourced (not executed) by supervisor.sh, status.sh, start.sh and stop.sh.
# The caller sets BASE to the relay directory first.
#
# Why this exists: the relay settings (LARK_BOT_OPEN_ID and friends) live in
# relay.env. If every entry point does not load them, a restart from a fresh
# shell (for example the health-check routine) loses LARK_BOT_OPEN_ID and the
# bot silently stops answering group @mentions.
export PATH="${HOME}/.local/bin:${PATH}"
# relay.env: non-secret settings for this relay (LARK_BRAND, LARK_BOT_OPEN_ID, LARK_BOT_APP_ID,
# LARK_BOT_NAME, LARK_BOT_ENV, optional LARK_BOT_WEBHOOK_URL). Shell syntax; quote values with spaces.
if [[ -f "${BASE}/relay.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  . "${BASE}/relay.env"
  set +a
fi
# Per-bot isolated lark-cli store (CONFIG_DIR/DATA_DIR/LOG_DIR). The file must
# contain only those dir vars and PATH, never secrets. If it is missing,
# lark-cli falls back to ~/.lark-cli.
export LARK_BOT_ENV="${LARK_BOT_ENV:-${HOME}/.lark-bots/lark-bot/env.sh}"
if [[ -f "${LARK_BOT_ENV}" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "${LARK_BOT_ENV}"
  set +a
fi
