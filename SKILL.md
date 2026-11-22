---
name: grokbot-lark
description: Connect a Grok Bot to Feishu or Lark over a long-lived lark-cli event connection, forward inbound messages to a handler webhook, and reply as the bot only.
---

# grokbot-lark

Set up a bridge so a Grok Bot can receive Feishu (China) or Lark (international)
messages and reply as the bot. Inbound events come from a long-lived
`lark-cli event consume` connection on the machine running the relay — not
from Feishu/Lark HTTP callbacks. The relay adds a working reaction, resolves
sender identity, and POSTs a structured payload to the handler bot's webhook
routine. Replies go out only through `lark-bot-cli` (`--as bot`).

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

Create a new app by running:

```bash
lark-cli config init --new
```

Have the operator scan the QR code. This creates the app in their tenant.
Record the app ID (client ID) — it is needed for the scope-apply link and for
`LARK_BOT_APP_ID`. After the bot can receive a message, also set
`LARK_BOT_OPEN_ID` (from the developer console or from an inbound event) so
the relay can ignore the bot's own messages and detect group @mentions.

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

## 4. Optional user OAuth

The relay and the handler must stay on the bot identity. User OAuth is
optional and only for work that cannot be done as the bot. If you need it:

```bash
lark-cli auth login --domain im,docs,drive,wiki,markdown,base,sheets --no-wait --json
```

Show the QR code, and use `--device-code` when a device-code flow is required.
`lark-bot-cli` will still reject `--as user`. Do not point the handler at
user tokens.

## 5. Create the handler bot's webhook routine

In the **handler bot's own chat**, create a webhook-triggered routine. Use
`templates/handler-routine-prompt.md` as the prompt.

The webhook URL and bearer key appear only in that bot's Routines panel.
Have the operator copy them into `~/.lark-relay/webhook.env` (file mode
`600`), or collect them with a secret form. Never paste the key into chat.
If they paste a full `Authorization: Bearer <key>` header, store only the
key. See `templates/webhook.env.example`.

## 6. Start the relay (long connection)

Copy the contents of `relay/` to `~/.lark-relay`, make that directory mode
`700`, and copy `templates/webhook.env.example` to `~/.lark-relay/webhook.env`.
Export the variables in `templates/relay.env.example` (`LARK_BRAND`,
`LARK_BOT_OPEN_ID`, and optionally `LARK_BOT_APP_ID` / `LARK_BOT_NAME`).

Start the supervisor (it holds a lock so only one consumer runs):

```bash
~/.lark-relay/supervisor.sh
```

The supervisor starts `relay.py`, which runs a **single** consumer:

```bash
lark-bot-cli event consume im.message.receive_v1 --as bot
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

## 8. Handler routine behavior

The handler prompt in `templates/handler-routine-prompt.md` must include:

- Reply in the sender's language.
- Use only `lark-bot-cli` (bot identity). Reply with
  `im +messages-reply --message-id <message_id>`.
- On `missing_scope`, include `console_url` and remind the operator to
  publish a new app version.
- Refuse to bypass the wrapper, to use `--as user`, or to read `webhook.env`.
- Let the relay own the working-reaction lifecycle. For long tasks, send a
  short acknowledgement first and put the final result in the last reply.

## 9. End-to-end test

Have the operator DM the bot a short hello.

- A working reaction should appear on the inbound message within about a
  second (this is the relay, not the handler waking up).
- The handler should reply in the sender's language.
- The working reaction should then disappear.

If the bot cannot be found in the product, a version has not been published
yet, or the availability range does not include the operator.

## Gotchas

- **Do not** point the Feishu or Lark HTTP event callback at the Grok Bot
  webhook. Feishu/Lark should not POST to Cursor. The long connection
  (`event consume`) receives events; the Grok webhook exists only so the
  relay can wake the handler bot.
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
