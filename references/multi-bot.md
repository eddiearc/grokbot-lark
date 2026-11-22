# More than one bot on a machine

All Grok Bots of one account share one computer and one Linux user: the same home directory, the
same `PATH`, the same files. Separation between bots is therefore a convention, not a sandbox. A
process that knows the path can read another bot's store.

## What each bot needs of its own

| Per bot | Why |
| --- | --- |
| A Feishu/Lark app | The app is the bot's identity. Two bots on one app would answer as each other. |
| A `lark-cli` store: `~/.lark-bots/<bot>/` | Holds that app's credentials, locked to the bot identity with `strict-mode bot`. |
| A relay directory | Holds that bot's lock, state, spool, logs, and webhook key. |
| A handler routine and a health-check routine | The webhook wakes exactly one bot. |

## Setting up a second bot

Run the usual steps with a name and a relay directory of its own:

```bash
python3 scripts/onboard.py app   --bot sales-bot --name "<its display name>"
python3 scripts/onboard.py wait  --bot sales-bot
python3 scripts/onboard.py scopes --bot sales-bot
python3 scripts/onboard.py relay --bot sales-bot --relay-dir ~/.lark-relay-sales-bot --start
python3 scripts/onboard.py webhook --relay-dir ~/.lark-relay-sales-bot \
  --key-env LARK_BOT_WEBHOOK_KEY_SALES_BOT --url '<POST to URL>'
```

Saved secrets are environment variables of the whole computer, so ask for this bot's webhook key
under a name of its own (here `LARK_BOT_WEBHOOK_KEY_SALES_BOT`) and pass that name with
`--key-env`. `onboard.py relay` refuses to install into a relay directory that already belongs
to another bot.

In that bot's two routine instructions, replace `~/.lark-relay` with its relay directory. The
payload's `instructions` field already names the right store env file, because each relay reads
`LARK_BOT_ENV` from its own `relay.env`.

`status.sh` counts only the event consumer started by its own relay, so two healthy relays do
not report each other as duplicates.

## Rules for every bot

- Never change `strict-mode`, never run `lark-cli auth login`, never use `--profile`,
  `lark-cli profile use`, or `LARKSUITE_CLI_PROFILE`.
- Never point `LARK_BOT_ENV` or a `LARKSUITE_CLI_*_DIR` variable at another bot's store.
- Before every `lark-cli` call, `source` your own store env file. Without it `lark-cli` falls
  back to `~/.lark-cli`, which may belong to someone else.

Check which identity a shell would use:

```bash
source ~/.lark-bots/sales-bot/env.sh && lark-cli whoami        # identity "bot", source "strict_mode"
source ~/.lark-bots/sales-bot/env.sh && lark-cli config show   # this bot's app ID
```

## Where the credentials live

The relay runs unattended, so the app secret has to be readable without a person present. It is
kept in the store's data directory (`~/.lark-bots/<bot>/data`, mode `700`), written by
`lark-cli` itself. `env.sh` exports only the three store directories and `PATH`; it never holds
a secret.

Do not export `LARKSUITE_CLI_APP_ID` on its own. In `lark-cli` 1.0.100 an `APP_ID` in the
environment switches the credential source to the environment; without a fresh
`LARKSUITE_CLI_TENANT_ACCESS_TOKEN` next to it, `whoami` can still look ready while API calls
fail. Once a `lark-cli` release can mint the tenant token from `LARKSUITE_CLI_APP_ID` and
`LARKSUITE_CLI_APP_SECRET` alone, the credentials can move into each bot's own secrets and the
on-disk copy can go.
