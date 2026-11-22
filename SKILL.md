---
name: grokbot-lark
description: Connect a Grok Bot to Feishu or Lark over a long-lived lark-cli event connection, forward inbound messages to a handler webhook, and reply as the bot only.
---

# grokbot-lark

## Who uses this skill

Hand this skill to **one specific Grok Bot** — the handler. That bot installs
and maintains the bridge:

- the inbound relay (`lark-cli event consume` plus `relay.py`)
- the one-click bot-scope apply link, followed by a **published app version**
- the webhook handler routine that wakes it on each inbound message
- the health-check routine

It also **handles the messages**: the relay POSTs a payload to its webhook, and
it replies (and calls other APIs) with `lark-cli ... --as bot`. Other Grok Bots
on the same machine do not own this bridge unless you hand them the skill and
give them their own Feishu/Lark app and isolated store.

## Two paths

The handler has **two separate paths**. The inbound path is the relay: a
long-lived `lark-cli event consume`, then a webhook that wakes the bot. The
outbound path is the bot itself calling `lark-cli ... --as bot` (reply, docs,
Bitable, and so on). Do not point a Feishu/Lark HTTP event callback at the Grok
webhook.

```
Inbound path (relay wakes the bot):

  Feishu/Lark  --long connection-->  lark-cli event consume im.message.receive_v1 --as bot
                                 -->  relay.py  --webhook-->  handler bot

Outbound path (the bot calls lark-cli itself):

  handler bot  --  source <store-env> && lark-cli im +messages-reply --message-id <id> --as bot  -->  Feishu/Lark
               --  source <store-env> && lark-cli docs ... --as bot
               --  source <store-env> && lark-cli base ... --as bot
```

The relay adds a working reaction, resolves sender identity, and POSTs a
structured payload to the handler bot's webhook routine. The handler then talks
to Feishu/Lark with plain `lark-cli`, after sourcing its isolated store env
file.

## Multiple bots on one box

Every Grok Bot on this machine shares **one box and one Linux user**. They
share a filesystem, a `PATH`, and anything readable under that home directory.
This is **isolation, not a hard lock**.

Each bot that talks to Feishu/Lark must get:

- its **own Feishu/Lark app** (its own App ID and secret)
- its **own isolated lark-cli store**: `LARKSUITE_CLI_CONFIG_DIR`,
  `LARKSUITE_CLI_DATA_DIR`, and `LARKSUITE_CLI_LOG_DIR`, plus
  `strict-mode bot` set **inside that store**
- an env file (mode `600`) that exports **only those three dir vars and PATH**
  — never the app secret. Default layout:

  `~/.lark-bots/<bot-name>/{config,data,logs}` and
  `~/.lark-bots/<bot-name>/env.sh`

  Example: `templates/lark-cli-store.env.example`. Create one with
  `scripts/init-bot-store.sh` (secret on stdin).

The relay for that app, and every `lark-cli` call the handler makes, must
**source that env file** (or set `LARK_BOT_ENV` to it) so they never fall
through to `~/.lark-cli` or to another bot's store.

No bot may:

- change `strict-mode`
- switch profiles (`lark-cli profile use`, `--profile`, or
  `LARKSUITE_CLI_PROFILE`)
- point `LARK_BOT_ENV` / `LARKSUITE_CLI_*_DIR` at another bot's store
- log in a user (`lark-cli auth login`)

These rules are policy. On a shared Linux user they are not a hard sandbox:
a process can still read another bot's store if it knows the path. The relay
**must keep the app credentials on disk** in that store's keychain files
(under `DATA_DIR`, mode `600`, directory `700`) so `event consume` and API
calls can run unattended.

**Future upgrade:** once a lark-cli release mints a tenant access token from
`LARKSUITE_CLI_APP_ID` + `LARKSUITE_CLI_APP_SECRET` alone (upstream commit
`5a68e24`; **not** in 1.0.100), move the creds into each bot's own Grok
secrets and drop the on-disk keychain. Until then, do **not** export
`LARKSUITE_CLI_APP_ID` without also providing a fresh
`LARKSUITE_CLI_TENANT_ACCESS_TOKEN` — in 1.0.100 an env `APP_ID` switches
the credential source to `env`, `whoami` may still say ready, and API calls
fail. When that minting release is in use, env `APP_ID` beats `--profile`
and is the right way to pin a bot to its own app.

## 1. Ask which brand

Ask whether this is **international Lark** or **China Feishu**:

| | Brand | Developer console and APIs |
| --- | --- | --- |
| China | `feishu` | `open.feishu.cn` |
| International | `lark` / `larksuite` | `open.larksuite.com` |

Set `LARK_BRAND` to `feishu` or `larksuite`. Console links, scope-apply URLs,
and the default `console_url` in forwarded payloads all follow this choice.

## 2. Install lark-cli and create the app

Install the official CLI and put it on `PATH` (for example `~/.local/bin`):

```bash
npm install -g @larksuite/cli
```

Create a **dedicated isolated store** for this handler bot (do not share
`~/.lark-cli` with other bots). Pick a bot name, for example `lark-bot`, and
create the app into that store.

If the operator is creating a new app, they can still run
`lark-cli config init --new` **after** exporting the three dir vars (or
sourcing the env file once the directories exist). Prefer the helper, which
reads the app secret from stdin and never writes it to `env.sh`:

```bash
mkdir -p ~/.lark-bots/lark-bot/{config,data,logs}
chmod 700 ~/.lark-bots ~/.lark-bots/lark-bot ~/.lark-bots/lark-bot/{config,data,logs}
# If the app already exists, feed the secret privately:
printf '%s' "$APP_SECRET" | scripts/init-bot-store.sh lark-bot <APP_ID> feishu
# or, for a new app, export the dir vars and run:
#   source ~/.lark-bots/lark-bot/env.sh   # after the helper has written it
#   lark-cli config init --new
```

Have the operator scan the QR code when `config init --new` is used. Record
the app ID (client ID) — it is needed for the scope-apply link and for
`LARK_BOT_APP_ID`. After the bot can receive a message, also set
`LARK_BOT_OPEN_ID` (from the developer console or from an inbound event) so
the relay can ignore the bot's own messages and detect group @mentions.

`init-bot-store.sh` already runs `lark-cli config strict-mode bot --global`
**inside that store**. Do not log in a user in this store.

## 3. Apply all bot (tenant) scopes up front

Bot scopes come **only** from the developer console, and they take effect only
after a **published app version**. Do not wait until the first API call fails.

Generate a one-click apply link from this repo's default list:

```bash
python3 scripts/scope_link.py --app-id <APP_ID> --brand feishu
# or --brand larksuite for international Lark
```

The URL has the form:

```
https://open.feishu.cn/page/scope-apply?clientID=<APP_ID>&scopes=<comma list>
```

(`open.larksuite.com` when the brand is Lark.) The comma-separated list comes
from `scopes/bot-scopes.txt`. That list includes chat, docs, drive, wiki,
Bitable, sheets, cards, and these contact scopes:

- `contact:user.base:readonly`
- `contact:user.basic_profile:readonly`
- `contact:user.employee_id:readonly`
- `contact:user.email:readonly`
- `contact:department.base:readonly`

The contact scopes let the relay enrich each sender with email, `en_name`, and
`employee_id`. If they are missing, identity resolution **fails soft**: the
message is still forwarded, with `name_unresolved` when a display name could
not be resolved. You can also import `scopes/feishu-scopes-import.json` in the
console if the UI offers a batch import.

Have the operator confirm the apply page, then **create and publish a new
version**. Until a version is published, people in the tenant may also be
unable to find the bot.

## 4. Stay on the bot identity

Do **not** run `lark-cli auth login` in this bot's store (or on a shared
`~/.lark-cli` if one still exists). The lock is `strict-mode bot` **in the
isolated store**, plus the absence of a user token, plus sourcing that store's
env file so commands do not fall through to another profile. There is no
separate wrapper. User OAuth is incompatible with that lock.

Confirm with:

```bash
source "${LARK_BOT_ENV:-$HOME/.lark-bots/lark-bot/env.sh}" && lark-cli whoami
```

Identity should be `bot`, source `strict_mode`. `config show` should report
the isolated `CONFIG_DIR`, not `~/.lark-cli`.

## 5. Create the handler bot's webhook routine

In the **handler bot's own chat**, create a webhook-triggered routine. Use
`templates/handler-routine-prompt.md` as the prompt.

The webhook URL and bearer key appear only in that bot's Routines panel.
Have the operator copy them into `~/.lark-relay/webhook.env` (file mode
`600`), or collect them with a secret form. Never paste the key into chat.
If they paste a full `Authorization: Bearer <key>` header, store only the
key. See `templates/webhook.env.example`.

## 6. Start the relay (inbound path)

Copy the contents of `relay/` to `~/.lark-relay`, make that directory mode
`700`, and copy `templates/webhook.env.example` to `~/.lark-relay/webhook.env`.
Export the variables in `templates/relay.env.example` (`LARK_BRAND`,
`LARK_BOT_OPEN_ID`, and optionally `LARK_BOT_APP_ID` / `LARK_BOT_NAME`).
Point the supervisor at this bot's store by installing
`~/.lark-bots/lark-bot/env.sh` (see `templates/lark-cli-store.env.example`)
or by setting `LARK_BOT_ENV` to that file. `supervisor.sh` and `status.sh`
source it automatically when present, and `relay.py` / `cliutil.py` load the
same dir vars so `event consume` and reaction/identity calls use the isolated
store.

Start the supervisor (it holds a lock so only one consumer runs):

```bash
~/.lark-relay/supervisor.sh
```

The supervisor starts `relay.py`, which runs a **single** inbound consumer:

```bash
lark-cli event consume im.message.receive_v1 --as bot
```

Keep the consumer's **stdin open**. Do not attach `event consume` itself to
`/dev/null`; an unbounded consume exits if stdin is closed. `supervisor.sh`
and `relay.py` already do this correctly.

The relay:

- Forwards direct messages, and group messages that @mention the bot.
- Immediately adds a working reaction (Typing, with fallbacks) and records
  the job in `state.db`.
- Builds a payload with sender identity, chat name, mentions, a short
  summary, and handling instructions (no `is_owner` flag).
- POSTs the payload to the handler webhook. If the webhook is not configured
  yet, events are spooled and flushed later.
- Clears the reaction when it sees the bot reply, or swaps it for an error
  reaction after 10 minutes with no reply, or if the webhook post fails.
  Jobs survive a relay restart.

## 7. Health-check routine

Create a routine that runs **every 30 minutes**, all day (messages can arrive
at any time). Use `templates/health-check-routine-prompt.md`.

It should run `~/.lark-relay/status.sh`, stay **silent when healthy**, and
restart the supervisor if the listener is down. Message the operator only
when a restart happened, a restart failed, or forwarding is erroring. Never
print webhook secrets.

## 8. Handler routine behavior (outbound path)

The handler prompt in `templates/handler-routine-prompt.md` must include:

- Reply in the sender's language.
- Call `lark-cli` as the bot, after sourcing this bot's store env file:

  ```bash
  source "${LARK_BOT_ENV:-$HOME/.lark-bots/lark-bot/env.sh}" && lark-cli im +messages-reply --message-id <message_id> --as bot
  ```

  The same pattern applies to docs, Bitable, and other APIs:
  `source … && lark-cli <command> ... --as bot`.
- On `missing_scope`, include `console_url` and remind the operator to
  publish a new app version.
- Do not read `webhook.env`.
- Let the relay own the working-reaction lifecycle. For long tasks, send a
  short acknowledgement first and put the final result in the last reply.

## 9. End-to-end test

Have the operator DM the bot a short hello.

- A working reaction should appear on the inbound message within about a
  second (this is the relay, not the handler waking up).
- The handler should reply in the sender's language, using
  `source … && lark-cli im +messages-reply --message-id <id> --as bot`.
- The working reaction should then disappear.

If the bot cannot be found in the product, a version has not been published
yet, or the availability range does not include the operator.

## Gotchas

- **Do not** point the Feishu or Lark HTTP event callback at the Grok Bot
  webhook. Feishu/Lark should not POST to Cursor. The long connection
  (`event consume`) is the inbound path; the Grok webhook exists only so the
  relay can wake the handler bot. Outbound calls are a separate path: the
  handler runs `lark-cli ... --as bot` itself.
- Bot (tenant) scopes come only from the developer console, and only after a
  published version. User OAuth cannot grant them.
- `docs` create drops `<bitable/>` tags. To embed a Bitable in a new
  document, add a children block with `block_type=18`.
- A bot can delete only reactions it added itself.
- Do not treat any Feishu/Lark user as a special owner. The payload has no
  `is_owner` field; every sender is just a user (name, open_id, chat).
- Contact scopes enrich the sender with email, `en_name`, and `employee_id`.
  Without them, resolution fails soft and the message is still forwarded.
- Set `LARK_BOT_OPEN_ID` or group @mentions may not be detected.
- The identity lock is `strict-mode bot` in this bot's isolated store, plus
  no user login, plus sourcing that store's env file. Do not add a second
  wrapper around `lark-cli`.
- Do not export `LARKSUITE_CLI_APP_ID` on lark-cli 1.0.100 unless you also
  provide a fresh `LARKSUITE_CLI_TENANT_ACCESS_TOKEN`. Env `APP_ID` beats
  `--profile`, but 1.0.100 will not mint a token from the app secret alone.
