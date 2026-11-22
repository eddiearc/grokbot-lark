---
name: grokbot-lark
description: Connect this Grok Bot to Feishu (China) or Lark (international) so people can DM it or @mention it in groups and get replies as the bot. Use it when asked to set up, connect, or install the Feishu/Lark bridge, when a forwarded Feishu/Lark message wakes you through the handler webhook, and when the bridge needs a health check, a restart, an upgrade, more permissions, or a second bot on the same machine.
---

# grokbot-lark

You are the Grok Bot that owns this bridge. You install it, you keep it running, and you answer
the messages it forwards. The human is the **operator**: during setup they scan one QR code and
hand you one webhook URL and key. Everything else is yours to do.

```
Inbound    Feishu/Lark ──long connection──▶ relay (lark-cli event consume) ──webhook──▶ you
Outbound   you ── source <store env> && lark-cli … --as bot ──▶ Feishu/Lark
```

## Rules that always apply

- **Do not stop to ask.** Setup needs the operator twice: the scan in step 1 and the webhook in
  step 5. Never end your turn to wait for "done": `onboard.py wait` blocks until they finish.
- **Follow the `next:` line.** Every `scripts/onboard.py` command prints `key: value` lines and
  ends with `next:`, the exact step to take.
- **Bot identity only.** Never run `lark-cli auth login`, change `strict-mode`, switch profiles,
  or touch another bot's store. Before any `lark-cli` call, `source` this bot's store env file.
- **Secrets stay out of chat.** Never print, paste, or ask for an app secret or webhook key in
  chat. Never read `webhook.env` aloud. Use a secure secret card.
- **One handler routine, forever.** A new routine gets a new webhook URL and key. Edit the
  existing routine instead of creating another one.

## Set up

Run everything from the repository root (the folder that holds this file). If you only have
this text, get the repository first:

```bash
[ -d ~/grokbot-lark ] || git clone https://github.com/eddiearc/grokbot-lark ~/grokbot-lark
cd ~/grokbot-lark
command -v lark-cli >/dev/null || npm install -g @larksuite/cli
```

Needs Linux with `python3` (3.9+), `npm`, `flock`, `setsid`, and `ps`.

### 1. Create the app: one scan

```bash
python3 scripts/onboard.py app --name "<your own display name>"
```

- Add `--brand lark` only if the operator said Lark or "international". Feishu is the default.
- Add `--avatar <https URL>` if you have a public URL of your own avatar. Skip it otherwise.
- Do not ask the operator anything first. A name, scopes, and events are already in the link.

The output has `verification_url` and `qr_png`. **Send one message now** that contains the link
as text and the QR image, for example: "Open this link, or scan it with Feishu, and confirm."
Then, in the same turn, wait:

```bash
python3 scripts/onboard.py wait
```

| Exit | `status` | What to do |
| --- | --- | --- |
| 0 | `ready` | Go to step 2. |
| 2 | `waiting_for_scan` | Run `wait` again. Do not message the operator again. |
| 1 | `failed` | The link expired or was declined. Follow `next:` for a new link. |

That one scan creates the app under your name with the bot capability and the message event,
requests every scope in `scopes/bot-scopes.txt`, and publishes the app. The operator does no
console work. Step 2 confirms what was actually granted.

### 2. Check permissions

```bash
python3 scripts/onboard.py scopes
```

- `complete`: everything was granted. Say nothing about scopes.
- `partial` or `unknown`: the bridge works. Keep going; mention `apply_url` once, in your final
  setup message.
- `blocked`: the bridge cannot receive messages yet (missing scope, or the tenant admin has not
  approved the app). Send `apply_url` with its QR image, then run `scopes --wait 600`.

### 3. Install and start the relay

```bash
python3 scripts/onboard.py relay --start
```

Expect `relay: relay started …; long connection ready`. The relay lives in `~/.lark-relay` and
already knows your app ID and bot `open_id`. Until the webhook is saved it keeps messages in a
spool, so nothing is lost.

### 4. Create your two routines

Create both yourself. Do not ask the operator to write them.

| Routine | Trigger | Instruction |
| --- | --- | --- |
| `Lark message handler` | Webhook | The text of `templates/handler-routine-prompt.md` |
| `Lark relay health check` | Every 30 minutes | The text of `templates/health-check-routine-prompt.md` |

### 5. Connect the webhook

You cannot read your own webhook URL or key; only the operator can see them. Ask once:

> Open the **Lark message handler** routine on desktop. In its **Webhook** section, paste the
> **POST to** URL here, and save the **key** in the secure card I am showing you.

Request a secure secret card named `LARK_BOT_WEBHOOK_KEY` for the key. Then run:

```bash
python3 scripts/onboard.py webhook --url '<POST to URL>'
```

`status: saved` means done; the relay picks it up within seconds, with no restart. If it says
`key: missing`, your shell cannot see the secret: follow `next:`.

### 6. Verify and hand over

```bash
~/.lark-relay/status.sh
```

`status: healthy` means the bridge is live. Tell the operator in one message: the bot's name in
Feishu/Lark, "send me a DM to try it", and (only if step 2 was `partial`) the `apply_url`.

## When a message wakes you

The handler routine receives one JSON payload per message. Its `instructions` field holds the
exact reply command for this bot; use it as given.

| Field | Meaning |
| --- | --- |
| `message_id`, `chat_id`, `chat_type` | What to reply to. `chat_type` is `p2p` or `group`. |
| `text`, `summary`, `mentions` | The message as plain text, a one-line summary, who was @mentioned. |
| `sender` | `open_id`, and when resolvable `name`, `en_name`, `email`, `user_id`, `employee_no`. |
| `chat` | `chat_id`, `type`, and the group name when there is one. |
| `console_url` | Where the operator grants permissions for this app. |
| `instructions` | The reply command and the rules for this message. |

- Reply with `im +messages-reply --message-id <message_id>` so the relay can match your answer.
  In a group, a plain send is never matched and the message ends with an error reaction.
- The relay owns the "working" reaction. Do not add or remove it. For a long task, send a short
  acknowledgement first, then the result.
- Reply in the sender's language. Every sender is an ordinary user; nobody is a special owner.
- A permission error (`app_scope_not_applied` or `missing_scope`) carries a `console_url`. Give
  that link to the sender and say which scope is missing.

## Operate

| Task | Command |
| --- | --- |
| Health report | `~/.lark-relay/status.sh` (ends with `status:` and one `problem:` line per fault) |
| Start / stop | `~/.lark-relay/start.sh`, `~/.lark-relay/stop.sh` |
| Upgrade | `git pull`, then `python3 scripts/onboard.py relay --start` |
| Re-check permissions | `python3 scripts/onboard.py scopes` |
| Change the webhook | `python3 scripts/onboard.py webhook --url '<new URL>'` |
| Logs | `~/.lark-relay/logs/relay.jsonl`, `consume.stderr.log`, `supervisor.log` |

`start.sh` returns as soon as the long connection is ready and is safe to run twice. Never run
`supervisor.sh` directly from a routine: it does not return.

## Gotchas

- Do not point a Feishu/Lark HTTP event callback at the handler webhook. Messages arrive over the
  long connection only.
- A computer update, Recover, or Reset removes installed packages and stops the relay. The health
  check reinstalls `lark-cli` and starts the relay again; files under `~` stay.
- The handler webhook is limited to 200 runs an hour. Past that, messages wait in the spool.
- A bot can delete only reactions it added itself.
- `docs` create drops `<bitable/>` tags. To embed a Bitable in a new document, add a child block
  with `block_type=18`.
- A second bot on this machine needs its own app, store, relay directory, and routines: pass
  `--bot <name>` to every `onboard.py` command. See `references/multi-bot.md`.

## Reference

| File | Read it when |
| --- | --- |
| `references/onboarding.md` | The scan link misbehaves, the operator already has an app, or you need to know what the link carries. |
| `references/architecture.md` | You need the payload schema, the reaction lifecycle, file locations, or limits. |
| `references/troubleshooting.md` | `status.sh` reports a problem, or messages get no reply. |
| `references/multi-bot.md` | More than one bot shares this machine. |
