# grokbot-lark

A [Grok Bot](https://grok.com) skill that bridges **Feishu** (China) or **Lark**
(international) to a handler bot over a long-lived [`lark-cli`](https://www.npmjs.com/package/@larksuite/cli)
event connection.

**Who uses it.** Hand this skill to **one specific Grok Bot** (the handler).
That bot installs and maintains the bridge — the relay, the one-click bot-scope
apply link plus a published app version, the webhook handler routine, and the
health check — and it handles the inbound messages. Other bots on the same
machine should not share this handler's Feishu/Lark app or lark-cli store.

The handler has **two separate paths**:

```
Inbound path (relay wakes the bot):

  Feishu/Lark  --long connection-->  lark-cli event consume im.message.receive_v1 --as bot
                                 -->  relay.py  --webhook-->  handler bot

Outbound path (the bot calls lark-cli itself):

  handler bot  --  source <store-env> && lark-cli im +messages-reply --message-id <id> --as bot  -->  Feishu/Lark
               --  source <store-env> && lark-cli docs ... --as bot
               --  source <store-env> && lark-cli base ... --as bot
```

The relay on your machine consumes `im.message.receive_v1` as the bot, adds a
working reaction, resolves who sent the message, and POSTs a structured payload
to the handler bot's webhook routine. The handler then calls
`lark-cli ... --as bot` itself (reply, docs, Bitable, and so on), after
sourcing its isolated store env file. There is no wrapper around `lark-cli`.
Identity is locked **inside that store** with `strict-mode bot` and no user
login. Confirm with:

```bash
source "${LARK_BOT_ENV:-$HOME/.lark-bots/lark-bot/env.sh}" && lark-cli whoami
```

(identity should be `bot`, source `strict_mode`).

### Multiple bots on one box

Every Grok Bot on the machine shares **one box and one Linux user**. This is
**isolation, not a hard lock**. Each bot that talks to Feishu/Lark needs its
own app and its own isolated lark-cli store (`LARKSUITE_CLI_CONFIG_DIR`,
`DATA_DIR`, `LOG_DIR`, plus `strict-mode bot` in that store), sourced from
its env file (`templates/lark-cli-store.env.example`, created by
`scripts/init-bot-store.sh`). The env file exports only those dir vars and
`PATH` — never the app secret.

No bot may change `strict-mode`, switch profiles, point at another bot's
store, or log in a user. On a shared user these rules are policy, not a
sandbox: a process can still read another store if it knows the path. The
relay **must keep the app credentials on disk** in that store so the long
connection can run unattended.

**Future upgrade:** once a lark-cli release mints a tenant access token from
`LARKSUITE_CLI_APP_ID` + `LARKSUITE_CLI_APP_SECRET` (upstream commit
`5a68e24`; not in 1.0.100), move the creds into each bot's own Grok secrets.
Until then, do not export `APP_ID` without a fresh
`LARKSUITE_CLI_TENANT_ACCESS_TOKEN`. When that release is in use, env
`APP_ID` beats `--profile`.

Start from [`SKILL.md`](SKILL.md) and the templates in `templates/`.

## What's in this repo

| Path | Purpose |
| --- | --- |
| `SKILL.md` | Step-by-step skill for a Grok Bot (or a human operator) |
| `relay/` | Long-connection consumer, supervisor, health `status.sh`, reaction lifecycle, identity enrichment |
| `scripts/scope_link.py` | Build a one-click bot-scope apply URL from `scopes/bot-scopes.txt` |
| `scripts/init-bot-store.sh` | Create an isolated lark-cli store (secret on stdin; writes dir-only `env.sh`) |
| `scopes/bot-scopes.txt` | Default bot (tenant) scopes, including contact scopes |
| `scopes/feishu-scopes-import.json` | Same scopes in a console batch-import shape |
| `templates/` | Handler prompt, health-check prompt, `webhook.env`, relay env, and isolated-store env examples |

## Quick start

1. Ask whether you are on China Feishu (`open.feishu.cn`) or international Lark (`open.larksuite.com`).
2. Install `@larksuite/cli`. Create this bot's isolated store with `scripts/init-bot-store.sh` (existing app, secret on stdin) or, after exporting the dir vars, `lark-cli config init --new` (QR) to create an app.
3. Apply all bot scopes, then publish a version:

   ```bash
   python3 scripts/scope_link.py --app-id <APP_ID> --brand feishu
   ```

4. The helper already sets `strict-mode bot` in that store. Source `~/.lark-bots/lark-bot/env.sh` (or `$LARK_BOT_ENV`) before every `lark-cli` call. Do not log in a user.
5. Create a webhook routine in the **handler bot's** chat (`templates/handler-routine-prompt.md`). Put the URL and key in `~/.lark-relay/webhook.env` (mode `600`).
6. Copy `relay/` to `~/.lark-relay` and start `supervisor.sh`. It sources the store env file, runs a single inbound `lark-cli event consume im.message.receive_v1 --as bot`, and keeps stdin open.
7. Add a health-check routine every 30 minutes (`templates/health-check-routine-prompt.md`). It stays silent when healthy.
8. DM the bot to test. A working reaction should appear immediately; the handler replies with `source … && lark-cli im +messages-reply --message-id <id> --as bot`; the reaction is then removed.

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

`grokbot-lark` 是一套给 Grok Bot 用的飞书 / Lark 桥接技能。

**怎么用。** 把这套 skill 交给**某一个** Grok Bot（处理机器人）。由它来安装和维护整座桥：中继、一键开通权限并发布应用版本、webhook 处理 routine、健康检查；入站消息也由它处理。同一台机器上的其他 bot 不要共用它的飞书应用或 lark-cli 配置。

机器人有两条互不影响的路径：

- **入站**：机器上的中继用 `lark-cli event consume ... --as bot` 长连接收消息，加上「处理中」表情、解析发送者身份，再通过 webhook 唤醒处理机器人。
- **出站**：处理机器人先载入自己的隔离配置，再调用 `lark-cli ... --as bot` 回复或操作文档、多维表格等，例如 `source ~/.lark-bots/lark-bot/env.sh && lark-cli im +messages-reply --message-id <id> --as bot`。

**同一台机器上的多个 bot。** 所有 Grok Bot 共用一台机器、同一个 Linux 用户。这是隔离，不是硬锁。每个要对接飞书 / Lark 的 bot 都要有自己的应用，以及自己的隔离 lark-cli 存储（`CONFIG_DIR` / `DATA_DIR` / `LOG_DIR`，并在该存储里打开 `strict-mode bot`），通过自己的 env 文件载入。env 文件只导出这三个目录变量和 `PATH`，不要写 app secret。任何 bot 都不得改 strict-mode、切换 profile、指向别人的存储，或登录用户身份。在共享用户下这些只是约定：知道路径仍可能读到别人的存储。中继必须把应用凭证留在该存储的磁盘上，长连接才能无人值守运行。

等将来某个 lark-cli 版本能仅用 `LARKSUITE_CLI_APP_ID` + `APP_SECRET` 换取 tenant access token（上游提交 `5a68e24`，1.0.100 还没有）时，再把凭证迁到每个 bot 自己的 Grok 密钥里。在那之前，不要只导出 `APP_ID` 却不提供有效的 `LARKSUITE_CLI_TENANT_ACCESS_TOKEN`。新版本里，环境变量里的 `APP_ID` 会覆盖 `--profile`。

身份锁在该 bot 的隔离存储里：`strict-mode bot`，并且不要登录用户。没有额外的包装器。详细步骤、注意点和模板见上面的英文说明与 `SKILL.md`。
