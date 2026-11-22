# grokbot-lark

A [Grok Bot](https://grok.com) skill that bridges **Feishu** (China) or **Lark**
(international) to a handler bot over a long-lived [`lark-cli`](https://www.npmjs.com/package/@larksuite/cli)
event connection.

The bot that owns this skill has **two separate paths**:

```
Inbound path (relay wakes the bot):

  Feishu/Lark  --long connection-->  lark-cli event consume im.message.receive_v1 --as bot
                                 -->  relay.py  --webhook-->  handler bot

Outbound path (the bot calls lark-cli itself):

  handler bot  --  lark-cli im +messages-reply --message-id <id> --as bot  -->  Feishu/Lark
               --  lark-cli docs ... --as bot
               --  lark-cli base ... --as bot
```

The relay on your machine consumes `im.message.receive_v1` as the bot, adds a
working reaction, resolves who sent the message, and POSTs a structured payload
to the handler bot's webhook routine. The handler then calls
`lark-cli ... --as bot` itself (reply, docs, Bitable, and so on). There is no
wrapper around `lark-cli`. Identity is locked with:

```bash
lark-cli config strict-mode bot --global
# If a user is already logged in:
lark-cli auth logout
```

Do not log in a user on that machine. Confirm with `lark-cli whoami`
(identity should be `bot`, source `strict_mode`).

Start from [`SKILL.md`](SKILL.md) and the templates in `templates/`.

## What's in this repo

| Path | Purpose |
| --- | --- |
| `SKILL.md` | Step-by-step skill for a Grok Bot (or a human operator) |
| `relay/` | Long-connection consumer, supervisor, health `status.sh`, reaction lifecycle, identity enrichment |
| `scripts/scope_link.py` | Build a one-click bot-scope apply URL from `scopes/bot-scopes.txt` |
| `scopes/bot-scopes.txt` | Default bot (tenant) scopes, including contact scopes |
| `scopes/feishu-scopes-import.json` | Same scopes in a console batch-import shape |
| `templates/` | Handler prompt, health-check prompt, `webhook.env` and relay env examples |

## Quick start

1. Ask whether you are on China Feishu (`open.feishu.cn`) or international Lark (`open.larksuite.com`).
2. Install `@larksuite/cli` and run `lark-cli config init --new` (QR) to create the app.
3. Apply all bot scopes, then publish a version:

   ```bash
   python3 scripts/scope_link.py --app-id <APP_ID> --brand feishu
   ```

4. Lock the CLI to the bot: `lark-cli config strict-mode bot --global`. Do not log in a user (or run `lark-cli auth logout` if one is already logged in).
5. Create a webhook routine in the **handler bot's** chat (`templates/handler-routine-prompt.md`). Put the URL and key in `~/.lark-relay/webhook.env` (mode `600`).
6. Copy `relay/` to `~/.lark-relay` and start `supervisor.sh`. It runs a single inbound `lark-cli event consume im.message.receive_v1 --as bot` and keeps stdin open.
7. Add a health-check routine every 30 minutes (`templates/health-check-routine-prompt.md`). It stays silent when healthy.
8. DM the bot to test. A working reaction should appear immediately; the handler replies with `lark-cli im +messages-reply --message-id <id> --as bot`; the reaction is then removed.

## Contact scopes and identity

The default scope list includes:

- `contact:user.base:readonly`
- `contact:user.basic_profile:readonly`
- `contact:user.employee_id:readonly`
- `contact:user.email:readonly`
- `contact:department.base:readonly`

These let the relay enrich the sender with email, `en_name`, and `employee_id`
(plus a display name). If the scopes are missing, enrichment **fails soft**:
the event is still forwarded, and the payload may set `name_unresolved`.

The payload never includes an `is_owner` flag. Every sender is an ordinary
Feishu/Lark user.

## Working reaction

The **relay** owns the reaction lifecycle (not the handler):

1. On an inbound message it should handle, it immediately adds a working emoji
   (`Typing`, then `OnIt` / `OK` if needed) and stores the job in `state.db`.
2. When it sees the bot reply to that message (or, in a DM, a later bot
   message), it removes the reaction.
3. If the webhook post fails, or 10 minutes pass with no reply, it swaps the
   working emoji for an error emoji.
4. After a restart it reconciles `state.db` and re-adds a missing reaction for
   jobs that are still open.

A bot can delete only reactions it added itself.

## Tests

```bash
python3 -m unittest discover -s relay/tests -v
```

## License

MIT. See [LICENSE](LICENSE).

---

## 中文简介

`grokbot-lark` 是一套给 Grok Bot 用的飞书 / Lark 桥接技能。机器人有两条互不影响的路径：

- **入站**：机器上的中继用 `lark-cli event consume ... --as bot` 长连接收消息，加上「处理中」表情、解析发送者身份，再通过 webhook 唤醒处理机器人。
- **出站**：处理机器人自己调用 `lark-cli ... --as bot` 回复或操作文档、多维表格等，例如 `lark-cli im +messages-reply --message-id <id> --as bot`。

身份锁在 CLI 层：`lark-cli config strict-mode bot --global`，并且不要登录用户（若已登录则 `lark-cli auth logout`）。没有额外的包装器。详细步骤、注意点和模板见上面的英文说明与 `SKILL.md`。
