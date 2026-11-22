# Architecture

## Two paths

```
Inbound    Feishu/Lark ──long connection──▶ lark-cli event consume im.message.receive_v1 --as bot
                                              │ one JSON line per message
                                              ▼
                                           relay.py ──POST + Bearer key──▶ handler routine (wakes the bot)

Outbound   bot ── source <store env> && lark-cli im +messages-reply --message-id <id> --as bot ──▶ Feishu/Lark
```

The two paths share nothing but the app. Feishu/Lark never calls the handler webhook; only the
relay does. The bot never talks to the relay; it only calls `lark-cli`.

## Processes

```
start.sh ─▶ supervisor.sh        holds state/supervisor.lock, restarts relay.py (2 s, backing off to 60 s)
              └─ relay.py         one per relay directory
                   └─ lark-cli event consume …   one long connection; its stdin is a pipe relay.py keeps open
```

`lark-cli event consume` exits when its stdin closes, so it stops with `relay.py`. Stopping the
supervisor (`stop.sh`, or `kill <supervisor pid>`) stops all three.

## What gets forwarded

| Message | Forwarded |
| --- | --- |
| Direct message to the bot | Yes |
| Group message that @mentions the bot | Yes (needs `LARK_BOT_OPEN_ID`, which `onboard.py relay` sets) |
| Group message without an @mention | No |
| Any message sent by a bot or app, including this one | No |
| A `message_id` seen before (last 50,000 are remembered) | No |

For each forwarded message the relay:

1. adds a working reaction (`Typing`, falling back to `OnIt`, then `OK`) and records a job;
2. resolves the sender, the chat, and the mentioned users, caching each for six hours (five
   minutes when the name could not be resolved);
3. POSTs the payload to the webhook: 20 s timeout, three attempts.

If the webhook is not configured or the POST fails, the payload is appended to
`spool/pending.jsonl` and retried every two seconds, in order.

## Payload

```json
{
  "message_id": "om_…",
  "chat_id": "oc_…",
  "chat_type": "p2p",
  "sender_open_id": "ou_…",
  "sender": {"open_id": "ou_…", "name": "Alice", "en_name": "Alice", "email": "alice@example.com",
             "user_id": "…", "employee_no": "…"},
  "chat": {"chat_id": "oc_…", "type": "p2p", "name": "Direct message"},
  "mentions": [{"open_id": "ou_…", "name": "…"}],
  "text": "hello",
  "summary": "Alice in a direct message said: hello",
  "console_url": "https://open.feishu.cn/app/cli_…/auth",
  "instructions": "…the reply command for this bot and the rules…",
  "reaction_id": "…",
  "raw": {"…": "the event exactly as lark-cli emitted it"}
}
```

- `sender` always has `open_id`. The other fields appear when the contact scopes allow it.
  `name_unresolved: true` (on the payload and in `sender`) means no display name was found.
- `text` is `lark-cli`'s plain-text rendering. For an interactive card it is the raw card JSON.
- There is no owner flag. Every sender is an ordinary user.

## Working reaction

The relay owns the reaction on the sender's message. The handler never touches it.

| Job state | Reaction | Becomes |
| --- | --- | --- |
| `pending` | working emoji | `done` when a reply is seen; `error` after 10 minutes without one, or when the webhook POST fails |
| `error` | `ERROR` emoji | `done` when a reply is seen later; `abandoned` 6 hours after the message |
| `done` | removed | final |
| `abandoned` | `ERROR` emoji stays | final; the relay stops watching the chat |

Every 20 seconds the relay lists the last 50 messages of each chat that has an open job
(`im +chat-messages-list`) and looks for the bot's answer:

- a bot message whose `reply_to` is the job's message closes that job;
- inside a thread, a bot message that comes after the job's message closes that job;
- in a direct message only, a later bot message that replies to nothing closes the oldest job.

So the reaction disappears up to 20 seconds after the reply. After a restart the relay re-reads
its jobs from `state.db` and puts back a reaction that went missing.

## Files

```
<relay dir>/                      default ~/.lark-relay
  relay.env                       settings, loaded by every script on every start
  webhook.env                     webhook URL and key (mode 600), re-read every 2 s
  state.db                        jobs and the identity cache (SQLite)
  state/seen_message_ids.json     de-duplication
  state/supervisor.pid, relay.pid, supervisor.lock
  spool/pending.jsonl             payloads waiting for the webhook
  logs/relay.jsonl                one JSON line per relay event (forwarded, skipped, reaction_*, …)
  logs/consume.stderr.log         lark-cli's own output for the long connection
  logs/supervisor.log, supervisor.out, relay.out, relay.err
```

`relay.env` keys: `LARK_BOT_ENV` (the store env file), `LARK_BRAND` (`feishu` or `lark`),
`LARK_BOT_APP_ID`, `LARK_BOT_OPEN_ID`, `LARK_BOT_NAME`, and optionally `LARK_CONSOLE_URL` and
`LARK_BOT_WEBHOOK_URL`. It is shell syntax; quote values that contain spaces.

## Limits

- **Long threads.** `lark-cli` lists only the first 50 messages of a thread. An answer posted
  deeper than that in a thread (or in a topic group) is not matched, and the message ends with
  the `ERROR` reaction even though the reply was sent.
- **Any app's reply counts.** In a group with several bots, another bot replying to the same
  message closes this bot's job.
- **One message at a time.** Events are handled in order. While a webhook POST is retrying,
  later messages wait; `status.sh` shows the consumer's `dropped` counter if the event bus
  discards any.
- **Webhook rate limit.** Grok Bot starts at most 200 runs an hour per routine and 300 across
  all routines. Past that the POST is refused and messages wait in the spool.
- **Downtime can miss messages.** A message that arrives while the relay is not connected may
  never be delivered to it. Keep restarts short and let the health check bring the relay back.
- **Logs are not rotated.** Truncate the files under `logs/` when they grow large.
