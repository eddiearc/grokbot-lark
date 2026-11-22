# Troubleshooting

Start with the health report. It ends with `status: healthy`, or with `status: unhealthy` and one
`problem:` line per fault, each naming its fix.

```bash
~/.lark-relay/status.sh
```

## `problem:` lines

| Problem | Meaning | Fix |
| --- | --- | --- |
| `lark_cli_missing` | `lark-cli` is not on `PATH`, typically after a computer update, Recover, or Reset. | `npm install -g @larksuite/cli`, then `~/.lark-relay/start.sh`. |
| `listener_down` | The supervisor is not running. | `~/.lark-relay/start.sh` |
| `relay_crashing` | The supervisor is up but `relay.py` or its event consumer keeps exiting. | Read `logs/relay.err` and `logs/consume.stderr.log`. The usual causes are below. |
| `duplicate_consumer` | More than one event consumer runs under this relay. | `~/.lark-relay/stop.sh`, then `~/.lark-relay/start.sh`. |
| `never_ready` | The long connection has never come up. | Read `logs/consume.stderr.log`; then `python3 scripts/onboard.py scopes`. |
| `webhook_unconfigured` | No webhook URL or key. Messages are kept in the spool. | `SKILL.md` step 5. |
| `forwarding_stalled` | Messages have waited in the spool for more than a minute. | Check the URL and key, that the handler routine is not paused, and `webhook_post_failed` entries in `logs/relay.jsonl`. |

## Symptoms

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| The bot cannot be found in Feishu/Lark. | The app is not published yet (a tenant admin must approve it), or its availability range does not include that person. | The operator approves the app or widens the range in the developer console. |
| A DM gets no reaction at all. | The relay is down, or the app lacks the message scope or event. | `status.sh`, then `python3 scripts/onboard.py scopes`. |
| A reaction appears but no reply comes; `ERROR` after 10 minutes. | The webhook did not start a run, or the handler failed. | Look for `forwarded` or `webhook_post_failed` in `logs/relay.jsonl`, then at the handler routine's run history. |
| The reply arrived but the message still got `ERROR`. | The answer was not sent with `im +messages-reply --message-id <id>`, or it sits past the 50th message of a thread. | Reply to the message directly. See "Limits" in `architecture.md`. |
| Group @mentions are ignored; DMs work. | `LARK_BOT_OPEN_ID` is empty or wrong in `relay.env`. | `python3 scripts/onboard.py relay --start` rewrites it from the app. |
| The sender has no name (`name_unresolved`). | The contact scopes are missing. | `python3 scripts/onboard.py scopes`, give the operator `apply_url`. Names are retried after five minutes. |
| A `lark-cli` call fails with `app_scope_not_applied`. | The app does not have that scope. | Give the operator the `console_url` from the error. |
| Replies come from the wrong bot. | A `lark-cli` call ran without sourcing this bot's store env file. | Use the command in the payload's `instructions`. |
| `webhook_post_failed` with `http_401` or `http_403`. | Wrong or outdated key. | `python3 scripts/onboard.py webhook --url '<URL>'` with the current key. |
| `webhook_post_failed` during a burst of messages. | The handler routine reached its limit of 200 runs an hour. | Nothing to do: messages stay in the spool and are delivered later. |
| `start.sh` says "already running" but `status.sh` says `listener_down`. | The supervisor died and left `relay.py` holding the lock. | `pkill -f "$HOME/.lark-relay/relay.py"`, then `~/.lark-relay/start.sh`. |
| `consume.stderr.log` says another bus is already connected. | A second relay, or a manual `lark-cli event consume`, uses the same store. | Stop the other one. Each bot needs its own store and relay directory. |

## Useful commands

```bash
tail -n 50 ~/.lark-relay/logs/relay.jsonl            # what the relay did, one JSON line per event
tail -n 50 ~/.lark-relay/logs/consume.stderr.log     # lark-cli's view of the long connection
tail -n 20 ~/.lark-relay/logs/supervisor.log         # restarts and exit codes
source ~/.lark-bots/lark-bot/env.sh && lark-cli whoami   # must say identity "bot", source "strict_mode"
source ~/.lark-bots/lark-bot/env.sh && lark-cli event consume im.message.receive_v1 --as bot --dry-run
```

`relay.jsonl` event names worth knowing: `consumer_ready`, `forwarded`, `skipped` (with a
`reason`), `spooled_webhook_unconfigured`, `spooled_after_post_failure`, `webhook_post_failed`,
`reaction_added`, `reaction_complete`, `reaction_timeout`, `reaction_abandoned`.

## Starting over

```bash
~/.lark-relay/stop.sh
python3 scripts/onboard.py app --force --name "<your own display name>"   # new app, new link
```

`--force` replaces the app in this bot's store. The old app stays in the developer console until
the operator deletes it there. The handler routine and its webhook stay valid.
