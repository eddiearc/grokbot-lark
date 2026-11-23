# grokbot-lark

A skill that connects a [Grok Bot](https://docs.x.ai/grok-bot) to **Feishu** (China) or **Lark**
(international). People DM the bot or @mention it in a group; the bot answers as itself, with
its own app, name, and permissions.

The skill is written for the bot. You hand it over, the bot does the setup, and you confirm on
one page.

## Set it up

Tell your Grok Bot:

> Set up the grokbot-lark skill from https://github.com/eddiearc/grokbot-lark

What happens next:

1. The bot sends you a link and a QR code of the same link. Open either one and confirm. That
   creates the bot's app under the bot's own name and publishes it. The page asks for one
   permission, "Modify app info" (修改应用信息): the bot uses it to add the rest of its
   permissions itself, so this one confirmation is all it needs from you.
2. The bot installs a small relay on its computer and creates two routines for itself.
3. The bot asks for the webhook URL and key of one of those routines. A Grok Bot cannot read
   them itself, so you copy them from the routine's **Webhook** section into the two secure
   cards the bot shows you. Nothing is pasted into the chat.
4. Send the bot a DM in Feishu/Lark.

The bot waits for each of your steps on its own. You never have to tell it that you are done.

## How it works

```
Inbound    Feishu/Lark ──long connection──▶ relay (lark-cli event consume) ──webhook──▶ Grok Bot
Outbound   Grok Bot ── lark-cli im +messages-reply … --as bot ──▶ Feishu/Lark
```

- **Inbound.** A relay on the bot's computer holds a long connection to Feishu/Lark through the
  official [`lark-cli`](https://www.npmjs.com/package/@larksuite/cli). For each DM or @mention it
  adds a "working" reaction, looks up who is asking, and wakes the bot through a webhook routine.
  No public endpoint is needed.
- **Outbound.** The bot replies by calling `lark-cli` as the bot. When the relay sees the reply
  it removes the reaction; after ten minutes without one it shows an error reaction instead.
- **Identity.** Each bot gets its own app and its own isolated `lark-cli` store, locked to the
  bot identity. No user ever logs in.

## What is in the repository

| Path | Purpose |
| --- | --- |
| [`SKILL.md`](SKILL.md) | The bot's operating manual: setup, handling messages, operations. |
| [`references/`](references/) | Onboarding details, architecture, troubleshooting, several bots on one machine. |
| [`scripts/onboard.py`](scripts/onboard.py) | The setup tool the bot runs: `app`, `wait`, `scopes`, `relay`, `webhook`. |
| [`scripts/scope_link.py`](scripts/scope_link.py) | Builds permission links from the scope list, for adding scopes by hand. |
| [`relay/`](relay/) | The relay and its `start.sh`, `stop.sh`, and `status.sh`. |
| [`scopes/`](scopes/) | The bot scopes the app is given. |
| [`templates/`](templates/) | Instructions for the two routines, and example env files. |

## Requirements

The bot's computer needs Linux, `python3` 3.9 or newer, `npm` (for `lark-cli`), and the standard
`flock`, `setsid`, and `ps` tools. Nothing else is installed; the relay uses only the Python
standard library.

## Good to know

- **Permissions.** The bot gives its app every scope in
  [`scopes/bot-scopes.txt`](scopes/bot-scopes.txt): chat, contacts, documents, Base, sheets, wiki,
  and drive. Four are left out on purpose and listed at the end of that file: urgent phone calls
  and SMS, document ownership transfer, and document deletion. Edit the file before setup to ask
  for more or less.
- **The app can change its own permissions.** That is what "Modify app info" allows, and it is
  how the bot adds scopes without sending you to the developer console. The app keeps it, so a
  later version of this skill can add a scope the same way. If you would rather confirm every
  change yourself, remove it on the app's permission page; the bot then sends you a link to
  confirm whenever a scope is missing.
- **Who can use the bot.** Anyone who can DM the bot or @mention it can give it work, and the bot
  treats every sender as an ordinary user. Set the app's availability range in the developer
  console to limit who that is.
- **Where secrets live.** The app secret stays in the bot's `lark-cli` store and the webhook key
  in `~/.lark-relay/webhook.env`, both readable only by the bot's user. The relay runs between
  bot runs, so it needs them on disk.
- **Limits.** A Grok Bot routine starts at most 200 runs an hour, so that is the ceiling on
  answered messages. See [`references/architecture.md`](references/architecture.md) for the rest.

## Development

```bash
python3 -m unittest discover -s relay/tests -v
python3 -m unittest discover -s scripts/tests -v
bash tests/e2e/run.sh        # Linux only; runs the whole flow against a fake lark-cli
```

The tests use a fake `lark-cli` and never touch a real Feishu/Lark account.

## License

MIT. See [LICENSE](LICENSE).
