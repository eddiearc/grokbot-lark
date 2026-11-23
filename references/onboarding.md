# Onboarding in detail

`SKILL.md` gives the six setup steps. This page explains what happens behind them, and what to do
when a step does not go the usual way.

## What the operator gets

`scripts/onboard.py app` starts `lark-cli config init --new` in this bot's isolated store and
takes the verification link it prints. That link opens `/page/cli`, a page that reads only the
registration code: it ignores a name, an avatar, and permissions. So `onboard.py` moves the
link to `/page/launcher`, the page the platform itself names for this registration and the one
the official SDK's `registerApp` flow uses (`larksuite/node-sdk`, `scene/registration`). The
registration code and everything else in the link stay as `lark-cli` printed them, and
`lark-cli` receives the result exactly as before. Then it appends the parameters that page
understands:

| Parameter | Value | Effect on the page |
| --- | --- | --- |
| `name` | `--name` (default: the `--bot` value) | Pre-fills the app name. The operator can still edit it. |
| `desc` | `--desc` (default: `Grok Bot`) | Pre-fills the description. |
| `avatar` | each `--avatar` (up to 6) | Offers these images as the app icon. Each must be a public `https` image URL. |
| `addons` | scopes from `scopes/bot-scopes.txt` plus the event `im.message.receive_v1` | Adds them on top of the page's default template. |
| `createOnly` | `true` | Hides "use an existing app", so the scan always creates a new app. |
| `tp` | `lark_cli_template` | Keeps `lark-cli`'s default permission template when the tenant ignores `addons`. |

`addons` is compact JSON, gzipped, base64-encoded with the URL-safe alphabet and no padding:

```json
{"scopes": {"tenant": ["im:message", "…"], "user": []},
 "events": {"items": {"tenant": ["im.message.receive_v1"], "user": []}}}
```

With `addons` the link is more than a kilobyte of base64. An agent that writes such a link into
a chat message corrupts it often enough to matter, and a corrupted `addons` is ignored by the
page without any error. So the full link exists in one place only:

| Output | Carries | Sent how |
| --- | --- | --- |
| `qr_png` | The full link: name, description, avatar, scopes, event | Attached as an image. A tool generates it from the exact link. |
| `verification_url` | The same link without `addons` | Pasted as text. It is short. |

After the operator confirms, the page creates the app, adds the bot capability, the scopes and
the event, creates a version, and publishes it. `lark-cli` receives the app ID and secret and
stores them in the bot's store. `onboard.py wait` then locks the store to the bot identity
(`strict-mode bot`), reads the bot's `open_id`, and writes `~/.lark-bots/<bot>/app.json`.

## When the result differs

| What you see | Why | What to do |
| --- | --- | --- |
| `scopes` says `partial` | The operator opened the link instead of scanning, so only the page's default template was applied. Or the tenant does not have the "extra config" feature and the page ignored `addons`. Scope names the tenant cannot grant are also dropped silently. | The bridge works with the default template. Attach `apply_qr_png` once, in the final setup message. |
| `scopes` says `blocked` | The published version lacks the scope or event that `lark-cli event consume` needs, or a tenant admin has not approved the app yet. | Send `apply_qr_png`, then run `scopes --wait 600`. Repeat the wait until it clears. |
| `scopes` says `unknown` | The app could not read its own scope list, usually because `application:application:self_manage` is missing. | Continue. Attach `apply_qr_png` once at the end. |
| `wait` says `failed`, reason `expired` | The link was not used in time (about an hour at the time of writing). | Run the command on the `next:` line and send the new code and link. |
| `wait` says `failed`, reason `denied` | The operator declined on the page. | Ask whether they want to try again before sending a new code. |
| `qr_png: unavailable (…)` | `lark-cli auth qrcode` failed. | Send `verification_url` alone. The app is created with the default permissions and `scopes` reports what is missing. |
| The page shows a generated default name instead of the bot's name | The link opened `/page/cli`: an older copy of this skill, or `--page cli`. | `git pull`, then `python3 scripts/onboard.py app --force --name "<name>"` for a new code and link. The operator can also just type the name on the page. |
| The page asks to confirm where the link came from | `/page/launcher` shows a safety notice for sources it does not list. | The operator confirms; the link came from their own bot. |

The QR code is dense because it carries every scope. `onboard.py` renders it at 1024 px. It
still decodes when shrunk to a quarter of that, but a phone camera needs it shown large: if a
thumbnail does not scan, the operator opens the image at full size.

## Adding permissions later

`python3 scripts/onboard.py scopes` compares the app's granted bot scopes with
`scopes/bot-scopes.txt`. For whatever is missing it prints:

- `apply_qr_png`: a QR code of the launcher link (`/page/launcher?clientID=…&addons=…`). One
  scan; Feishu shows the difference, the operator confirms, and Feishu publishes the new version
  itself.
- `permissions_page`: the app's permission page in the developer console
  (`/app/<app id>/auth`), short enough to paste, for adding scopes by hand. After that the
  operator must create and publish a new app version.

The launcher link itself is printed (`apply_url`, with the classic `fallback_url`) only when the
QR image could not be generated. Copy it exactly in that case.

To change what the bot asks for, edit `scopes/bot-scopes.txt` (one scope per line) and keep
`scopes/feishu-scopes-import.json` identical; a test compares the two.

## The operator already has an app

Import it instead of creating one. Ask for the App Secret through a secure secret card (never in
chat), then pipe it in:

```bash
printf '%s' "$LARK_APP_SECRET" | python3 scripts/onboard.py app --app-id cli_xxx
python3 scripts/onboard.py scopes
```

Add `--brand lark` for an international app. An imported app usually needs the bot capability,
the `im.message.receive_v1` event over a long connection, and most scopes: `scopes` reports
`blocked`, and its `apply_qr_png` adds the scopes and the event in one scan.

## Feishu or Lark

`--brand feishu` (default) builds links on `open.feishu.cn`; `--brand lark` builds them on
`open.larksuite.com`. `larksuite` and `international` are accepted as aliases for `lark`. The
brand that `lark-cli` records after the scan is the one the relay uses from then on.

## Running a step again

Every command is safe to repeat.

- `app` while a link is pending prints the same link again and starts nothing new.
- `app` on a configured store prints `status: ready`. Use `--force` to replace the app.
- `wait` on a configured store finalizes and prints `status: ready`.
- `relay` on an existing relay directory upgrades the code and keeps `relay.env`, `webhook.env`,
  logs, and state.

## The webhook

A Grok Bot cannot read the URL or key of its own webhook routine; they are shown only in the
routine's **Webhook** section on desktop. That makes this the one manual step:

1. The operator pastes the **POST to** URL in chat. The URL is useless without the key.
2. The operator saves the **key** in a secure secret card named `LARK_BOT_WEBHOOK_KEY`.
3. `python3 scripts/onboard.py webhook --url '<POST to URL>'` writes
   `<relay dir>/webhook.env` (mode `600`) from the URL and the `LARK_BOT_WEBHOOK_KEY` environment
   variable. It never prints either value.
4. The same command then sends one test call, a body of `{"type": "relay_test", …}`, and prints
   `test: accepted (HTTP 200)` when the webhook took it. That starts one run of the handler
   routine, whose instruction says to end a `relay_test` run without doing anything. A wrong URL
   or key shows up here (`status: rejected`) instead of at the first real message. `--no-test`
   skips the call.

`python3 scripts/onboard.py webhook --check` sends the same test call with the URL and key the
relay already uses, and writes nothing. Use it after an upgrade or when replies stop.

A secret saved through the card is added to the computer's environment, which every bot on the
machine shares. If the command reports `key: missing`, the secret did not reach your shell:

- a session that was already running when the secret was saved may not see it. Run the command
  again from a new shell or a new session;
- or pipe the key from wherever your runtime keeps it:
  `… | python3 scripts/onboard.py webhook --key-stdin --url '<URL>'`;
- or have the operator put the key in `<relay dir>/webhook.env` themselves
  (`LARK_BOT_WEBHOOK_KEY=<key>`).

A second bot on the same machine must use a secret name of its own, passed with `--key-env`, so
the two keys do not overwrite each other.

The relay re-reads `webhook.env` every two seconds and also accepts `LARK_BOT_WEBHOOK_URL` and
`LARK_BOT_WEBHOOK_KEY` from its environment when the file leaves them empty. Messages that
arrived before the webhook was saved wait in the spool and are delivered as soon as it is.

## What setup writes

```
~/.lark-bots/<bot>/
  config/ data/ logs/     lark-cli's isolated store (credentials live here, mode 700)
  env.sh                  exports the three store directories and PATH; no secrets
  app.json                app_id, brand, bot_open_id, bot_name; no secrets
~/.lark-relay/
  relay.env               LARK_BOT_ENV, LARK_BRAND, LARK_BOT_APP_ID, LARK_BOT_OPEN_ID, LARK_BOT_NAME
  webhook.env             LARK_BOT_WEBHOOK_URL, LARK_BOT_WEBHOOK_KEY (mode 600)
  *.py *.sh               the relay, copied from relay/
```
