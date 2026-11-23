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
| `addons` | the one scope `application:application:patch` | Adds it on top of the page's default template. The page lists it on a second screen as "Modify app info" (修改应用信息), marked sensitive. |
| `createOnly` | `true` | Hides "use an existing app", so the link always creates a new app. |
| `tp` | `lark_cli_template` | Names `lark-cli`'s app template, which the page uses when it gets no `addons` or ignores them. |

`addons` is compact JSON, gzipped, base64-encoded with the URL-safe alphabet and no padding:

```json
{"scopes": {"tenant": ["application:application:patch"], "user": []},
 "events": {"items": {"tenant": [], "user": []}}}
```

Why one scope and not the whole list:

- Every app template the page offers grants the same 35 bot scopes, the message event
  included. Only 27 of them are in `scopes/bot-scopes.txt`, so a link without `addons` always
  ends at 27 granted scopes, whatever `tp` says.
- A link that listed the other scopes would carry about a kilobyte of base64. An agent that
  writes such a link into a chat message corrupts it often enough to matter, and the page
  ignores a corrupted `addons` without any error.
- One scope keeps the link near 300 characters. `qr_png` is that same link as an image, so
  the operator gets the same app whether they click or scan.

After the operator confirms, the page creates the app with the bot capability and the event,
adds the template's scopes and this one, creates a version, and publishes it. `lark-cli`
receives the app ID and secret and stores them in the bot's store. `onboard.py wait` then locks
the store to the bot identity (`strict-mode bot`), reads the bot's `open_id`, and writes
`~/.lark-bots/<bot>/app.json`.

## How `scopes` adds the permissions

`python3 scripts/onboard.py scopes` runs three calls as the bot:

| Call | Purpose |
| --- | --- |
| `GET /open-apis/application/v6/scopes` | Every scope the app has asked for and whether the tenant has granted it. Needs no scope itself. |
| `PATCH /open-apis/application/v7/applications/<app id>/config` | Adds the scopes from `scopes/bot-scopes.txt` that the app has not asked for. Needs `application:application:patch`. |
| `POST /open-apis/application/v7/applications/<app id>/publish` | Only when scopes are still not granted after the first two: submits a release of the app. |

With the bot's token the last two calls can change only the bot's own app. A scope that needs
no review is granted the moment it is added, so a tenant that approves its own apps ends at
`status: complete` within seconds and never reaches the third call. Where a tenant admin
reviews releases, the command prints `review: release <version> submitted` and the scopes
arrive when the admin approves.

The app keeps `application:application:patch` afterwards. Running `scopes` again later, for
example after an upgrade that lists a new scope, adds it the same way.

## When the result differs

| What you see | Why | What to do |
| --- | --- | --- |
| `scopes` says `partial` and prints `grant_url` | The app does not hold `application:application:patch`: the tenant does not have the "extra config" feature and the page ignored `addons`, the link was altered on the way, the operator removed the permission, or the app was imported. | Send `grant_url` and `grant_qr_png` in one message, then run `scopes --wait 600`. The page behind the link shows that single permission and one button. |
| `scopes` prints `review:` | Some scopes take effect only with a release, and a tenant admin has to approve it. | Continue the setup. Tell the operator that the remaining permissions arrive when the admin approves the release. Run `scopes` again afterwards to confirm. |
| `scopes` prints `apply_error:` | The platform refused the change; the line carries its message. | Continue the setup. Give the operator `permissions_page`; they can import [`scopes/feishu-scopes-import.json`](../scopes/feishu-scopes-import.json) there. |
| `scopes` says `blocked` | `lark-cli event consume` cannot run: a scope or the event it needs is missing, or a tenant admin has not approved the app yet. | The `blocker:` lines name the cause. With a `grant_url`, send it and wait; otherwise tell the operator what is missing and run `scopes --wait 600`. |
| `scopes` says `unknown` | The scope status could not be read, for example while the app is not yet installed in the tenant. | Continue, and run `scopes` again later. |
| `wait` says `failed`, reason `expired` | The link was not used in time (about an hour at the time of writing). | Run the command on the `next:` line and send the new link and code. |
| `wait` says `failed`, reason `denied` | The operator declined on the page. | Ask whether they want to try again before sending a new link. |
| `qr_png: unavailable (…)` | `lark-cli auth qrcode` failed. | Send `verification_url` alone. It is the same link. |
| The page shows a generated default name instead of the bot's name | The link opened `/page/cli`: an older copy of this skill, or `--page cli`. | `git pull`, then `python3 scripts/onboard.py app --force --name "<name>"` for a new link. The operator can also just type the name on the page. |
| The page asks to confirm where the link came from | `/page/launcher` shows a safety notice (操作安全提示) for sources it does not list. | The operator confirms; the link came from their own bot. |

## Adding permissions later

To change what the bot asks for, edit `scopes/bot-scopes.txt` (one scope per line) and keep
`scopes/feishu-scopes-import.json` identical; a test compares the two. Then run
`python3 scripts/onboard.py scopes`: it adds the new scopes itself. It never removes a scope.

`permissions_page`, printed by every `scopes` run, is the app's permission page in the developer
console (`/app/<app id>/auth`), for looking at the scopes or changing them by hand.

`python3 scripts/scope_link.py --app-id <app id>` builds a link that lists every scope, for an
operator who wants to confirm them on a page instead. That link is long: send it as a QR image
(`lark-cli auth qrcode`), not as text.

## The operator already has an app

Import it instead of creating one. Ask for the App Secret through a secure secret card (never in
chat), then pipe it in:

```bash
printf '%s' "$LARK_APP_SECRET" | python3 scripts/onboard.py app --app-id cli_xxx
python3 scripts/onboard.py scopes
```

Add `--brand lark` for an international app. An imported app does not hold
`application:application:patch`, so `scopes` prints `grant_url`: one confirmation there, and the
command adds the scopes. It adds scopes only. The bot capability and the
`im.message.receive_v1` event over a long connection are the operator's to set in the developer
console; while they are missing, `scopes` reports `blocked` and its `blocker:` lines say which.

## Feishu or Lark

`--brand feishu` (default) builds links on `open.feishu.cn`; `--brand lark` builds them on
`open.larksuite.com`. `larksuite` and `international` are accepted as aliases for `lark`. The
brand that `lark-cli` records after the registration is the one the relay uses from then on.

## Running a step again

Every command is safe to repeat.

- `app` while a link is pending prints the same link again and starts nothing new.
- `app` on a configured store prints `status: ready`. Use `--force` to replace the app.
- `wait` on a configured store finalizes and prints `status: ready`.
- `relay` on an existing relay directory upgrades the code and keeps `relay.env`, `webhook.env`,
  logs, and state.

## The webhook

A Grok Bot cannot read the URL or key of its own webhook routine; they are shown only in the
routine's **Webhook** section on desktop. That makes this the one manual step, and both values
travel the same way:

1. The operator pastes **POST to** into a secure secret card named `LARK_BOT_WEBHOOK_URL`.
2. The operator pastes **key** into a second card named `LARK_BOT_WEBHOOK_KEY`.
3. `python3 scripts/onboard.py webhook` reads both from the environment and writes
   `<relay dir>/webhook.env` (mode `600`). It never prints either value.
4. The same command then sends one test call, a body of `{"type": "relay_test", …}`, and prints
   `test: accepted (HTTP 200)` when the webhook took it. That starts one run of the handler
   routine, whose instruction says to end a `relay_test` run without doing anything. A wrong URL
   or key shows up here (`status: rejected`) instead of at the first real message. `--no-test`
   skips the call.

The command is forgiving about how the values arrive. A card may hold the URL, the key, the
whole `Authorization: Bearer <key>` header, or both values separated by a space, in either
card: it tells them apart. `--url '<POST to URL>'` still works when the URL came some other way.

`python3 scripts/onboard.py webhook --check` sends the same test call with the URL and key the
relay already uses, and writes nothing. Use it after an upgrade or when replies stop.

A secret saved through a card is added to the computer's environment, which every bot on the
machine shares. If the command reports `status: incomplete`, a value did not reach your shell:

- a session that was already running when the secret was saved may not see it. Run the command
  again from a new shell or a new session;
- or pipe the key from wherever your runtime keeps it:
  `… | python3 scripts/onboard.py webhook --key-stdin --url '<URL>'`;
- or have the operator put the two lines in `<relay dir>/webhook.env` themselves
  (`LARK_BOT_WEBHOOK_URL=<url>` and `LARK_BOT_WEBHOOK_KEY=<key>`).

A second bot on the same machine must use secret names of its own, passed with `--url-env` and
`--key-env`, so the values of the two bots do not overwrite each other.

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
