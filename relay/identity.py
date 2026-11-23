#!/usr/bin/env python3
"""Resolve Feishu sender/chat/mention identities and build webhook payloads."""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable

from cliutil import run_cli_retry, store_env_path
from store import Store

BOT_OPEN_ID = os.environ.get("LARK_BOT_OPEN_ID", "").strip()
BOT_NAME = os.environ.get("LARK_BOT_NAME", "the bot").strip() or "the bot"
IDENTITY_TTL = 6 * 3600  # 6 hours
# A profile without a name is retried soon: the operator may grant the contact scopes any time.
UNRESOLVED_TTL = 300
# Feishu sometimes issues placeholder display names of this form.
GENERIC_NAME = re.compile(r"^用户\d+$")


def console_url() -> str:
    explicit = os.environ.get("LARK_CONSOLE_URL", "").strip()
    if explicit:
        return explicit
    brand = os.environ.get("LARK_BRAND", "feishu").strip().lower()
    if brand in {"lark", "larksuite", "international"}:
        host = "https://open.larksuite.com"
    else:
        host = "https://open.feishu.cn"
    app_id = os.environ.get("LARK_BOT_APP_ID", "").strip()
    if app_id:
        return f"{host}/app/{app_id}/auth"
    return f"{host}/app"


LogFn = Callable[..., None]


def _pick(*values: Any) -> Any:
    for v in values:
        if v is not None and v != "":
            return v
    return None


def _best_display_name(*sources: dict[str, Any] | None) -> str | None:
    i18n_candidates = []
    named = []
    generic = []
    for src in sources:
        if not src:
            continue
        i18n = src.get("sender_i18n_names") or src.get("i18n_names") or src.get("name_i18n") or {}
        if isinstance(i18n, dict):
            for k in ("zh_cn", "en_us", "ja_jp"):
                if i18n.get(k):
                    i18n_candidates.append(str(i18n[k]))
        for key in ("name", "en_name", "nickname", "display_name", "user_name"):
            v = src.get(key)
            if not v:
                continue
            if GENERIC_NAME.match(str(v)):
                generic.append(str(v))
            else:
                named.append(str(v))
    if i18n_candidates:
        return i18n_candidates[0]
    if named:
        return named[0]
    if generic:
        return generic[0]
    return None


def _user_from_get_user(envelope: dict[str, Any]) -> dict[str, Any]:
    data = envelope.get("data") or envelope
    user = data.get("user") if isinstance(data, dict) else None
    if not isinstance(user, dict):
        user = data if isinstance(data, dict) else {}
    out: dict[str, Any] = {}
    for k in (
        "open_id",
        "union_id",
        "user_id",
        "name",
        "en_name",
        "email",
        "enterprise_email",
        "employee_no",
        "employee_type",
        "employee_id",
        "nickname",
        "mobile",
        "department_ids",
        "job_title",
        "city",
        "country",
        "gender",
    ):
        if user.get(k) not in (None, "", []):
            out[k] = user[k]
    return out


def _sender_from_message(msg: dict[str, Any] | None) -> dict[str, Any]:
    if not msg:
        return {}
    sender = msg.get("sender") or {}
    if not isinstance(sender, dict):
        return {}
    out: dict[str, Any] = {}
    if sender.get("id"):
        out["open_id"] = sender.get("id") if sender.get("id_type") in (None, "open_id") else sender.get("id")
        if sender.get("id_type") == "union_id":
            out["union_id"] = sender.get("id")
        elif sender.get("id_type") == "user_id":
            out["user_id"] = sender.get("id")
        else:
            out["open_id"] = sender.get("id")
    if sender.get("name"):
        out["name"] = sender.get("name")
    if sender.get("sender_i18n_names"):
        out["sender_i18n_names"] = sender["sender_i18n_names"]
    if sender.get("en_name"):
        out["en_name"] = sender["en_name"]
    return out


def _chat_from_get(envelope: dict[str, Any], chat_id: str, chat_type: str | None) -> dict[str, Any]:
    data = envelope.get("data") or envelope
    if not isinstance(data, dict):
        data = {}
    name = _pick(data.get("name"), _best_display_name(data))
    typ = _pick(data.get("chat_mode"), data.get("chat_type"), chat_type)
    out = {"chat_id": chat_id}
    if typ:
        out["type"] = typ
    if name:
        out["name"] = name
    elif typ == "p2p":
        out["name"] = "Direct message"
    return out


class IdentityResolver:
    def __init__(self, store: Store, log: LogFn | None = None, ttl: float = IDENTITY_TTL):
        self.store = store
        self.log = log or (lambda *a, **k: None)
        self.ttl = ttl

    def resolve_user(self, open_id: str, message_id: str | None = None, hint: dict[str, Any] | None = None) -> dict[str, Any]:
        if not open_id:
            return {"name_unresolved": True}
        cached = self.store.cache_get("user", open_id)
        if cached:
            return dict(cached)

        profile: dict[str, Any] = {"open_id": open_id}
        get = run_cli_retry(
            ["contact", "+get-user", "--user-id", open_id, "--user-id-type", "open_id", "--json"],
            timeout=20,
            log=self.log,
            op="get_user",
        )
        if get.get("ok"):
            profile.update(_user_from_get_user(get))
        else:
            err = get.get("error") or {}
            self.log("identity_get_user_failed", open_id=open_id, error=err.get("subtype") or err.get("message"))
            if err.get("subtype") in {"missing_scope", "app_scope_not_applied"} or "missing_scopes" in err:
                profile["scope_hint"] = "contact:user.base:readonly / contact:user.basic_profile:readonly"

        msg_src = _sender_from_message(self._mget_one(message_id) if message_id else None)
        name = _best_display_name(profile, msg_src, hint)
        en_name = _pick(profile.get("en_name"), (msg_src.get("sender_i18n_names") or {}).get("en_us"))
        if name:
            profile["name"] = name
        if en_name:
            profile["en_name"] = en_name
        for k in ("union_id", "user_id", "email", "enterprise_email", "employee_no", "job_title"):
            if k not in profile and msg_src.get(k):
                profile[k] = msg_src[k]
        if not profile.get("open_id"):
            profile["open_id"] = open_id
        if not profile.get("name"):
            profile["name_unresolved"] = True
        else:
            profile["name_unresolved"] = False

        ttl = UNRESOLVED_TTL if profile["name_unresolved"] else self.ttl
        self.store.cache_put("user", open_id, profile, ttl)
        return profile

    def resolve_chat(self, chat_id: str, chat_type: str | None = None) -> dict[str, Any]:
        if not chat_id:
            return {"type": chat_type} if chat_type else {}
        cached = self.store.cache_get("chat", chat_id)
        if cached:
            if chat_type and not cached.get("type"):
                cached = dict(cached)
                cached["type"] = chat_type
            return cached
        out = {"chat_id": chat_id}
        if chat_type:
            out["type"] = chat_type
        get = run_cli_retry(
            ["im", "chats", "get", "--chat-id", chat_id, "--json"],
            timeout=20,
            log=self.log,
            op="chat_get",
        )
        if get.get("ok"):
            out = _chat_from_get(get, chat_id, chat_type)
        else:
            err = get.get("error") or {}
            self.log("identity_chat_get_failed", chat_id=chat_id, error=err.get("subtype") or err.get("message"))
            if chat_type == "p2p":
                out["name"] = "Direct message"
        if not out.get("type") and chat_type:
            out["type"] = chat_type
        self.store.cache_put("chat", chat_id, out, self.ttl)
        return out

    def resolve_mentions(self, mentions: list[Any] | None) -> list[dict[str, Any]]:
        result = []
        for m in mentions or []:
            if not isinstance(m, dict):
                continue
            mid = m.get("id") or m.get("open_id") or m.get("member_id")
            name = m.get("name")
            entry: dict[str, Any] = {}
            if mid:
                entry["open_id"] = mid
                if not name and mid != BOT_OPEN_ID:
                    cached = self.store.cache_get("user", mid)
                    if cached and cached.get("name"):
                        name = cached["name"]
                    else:
                        profile = self.resolve_user(mid)
                        name = profile.get("name")
                if mid == BOT_OPEN_ID and not name:
                    name = BOT_NAME
            if name:
                entry["name"] = name
            if entry:
                result.append(entry)
        return result

    def _mget_one(self, message_id: str | None) -> dict[str, Any] | None:
        if not message_id:
            return None
        env = run_cli_retry(
            ["im", "+messages-mget", "--message-ids", message_id, "--no-reactions", "--json"],
            timeout=20,
            log=self.log,
            op="messages_mget",
        )
        if not env.get("ok"):
            return None
        data = env.get("data") or env
        msgs = data.get("messages") if isinstance(data, dict) else None
        if isinstance(msgs, list) and msgs:
            return msgs[0] if isinstance(msgs[0], dict) else None
        return None


def build_instructions(message_id: str) -> str:
    mid = message_id or "<message_id>"
    return (
        "This is a Feishu/Lark message forwarded by the grokbot-lark relay. "
        "Reply as the bot. Source this bot's isolated lark-cli store first, then run: "
        f'source "{store_env_path().expanduser().absolute()}" && '
        f"lark-cli im +messages-reply --message-id {mid} --as bot ...\n"
        "Do not read or print webhook.env.\n"
        "The relay manages the working reaction itself and treats your reply as completion. "
        "For long tasks, send a short acknowledgement first and put the final result in the last reply.\n"
        "If a lark-cli call fails because a scope is missing (error subtype app_scope_not_applied "
        "or missing_scope), tell the sender which scope is missing, include the console_url from "
        "that error (or the console_url in this payload), and remind the operator to approve it "
        "and publish a new app version if the console asks for one. "
        f"Console: {console_url()}\n"
        "Reply in the sender's language. Treat every sender as an ordinary Feishu/Lark user; "
        "no sender is a special owner."
    )


def build_summary(text: str, sender: dict[str, Any], chat: dict[str, Any]) -> str:
    who = sender.get("name") or sender.get("open_id") or "unknown sender"
    typ = chat.get("type") or "chat"
    cname = chat.get("name")
    if typ == "p2p" or typ == "private":
        place = "a direct message"
    else:
        place = f'group chat "{cname}"' if cname else "a group chat"
    snippet = " ".join((text or "").split())
    if len(snippet) > 80:
        snippet = snippet[:77] + "..."
    if snippet:
        return f"{who} in {place} said: {snippet}"
    return f"{who} sent a message in {place}"


def build_payload(
    event: dict[str, Any],
    resolver: IdentityResolver,
    reaction_id: str | None = None,
) -> dict[str, Any]:
    message_id = event.get("message_id") or ""
    chat_id = event.get("chat_id") or ""
    chat_type = event.get("chat_type") or ""
    sender_open_id = event.get("sender_id") or event.get("sender_open_id") or ""
    text = event.get("content") if isinstance(event.get("content"), str) else ""
    if not text and isinstance(event.get("text"), str):
        text = event["text"]

    sender = resolver.resolve_user(sender_open_id, message_id=message_id)
    # Keep payload sender compact but include extra identity when present.
    sender_out: dict[str, Any] = {"open_id": sender.get("open_id") or sender_open_id}
    for k in ("union_id", "user_id", "name", "en_name", "email", "enterprise_email", "employee_no", "job_title"):
        if sender.get(k):
            sender_out[k] = sender[k]
    name_unresolved = bool(sender.get("name_unresolved")) or not sender_out.get("name")
    if name_unresolved:
        sender_out["name_unresolved"] = True

    chat = resolver.resolve_chat(chat_id, chat_type)
    chat_out: dict[str, Any] = {"chat_id": chat.get("chat_id") or chat_id}
    if chat.get("name"):
        chat_out["name"] = chat["name"]
    if chat.get("type") or chat_type:
        chat_out["type"] = chat.get("type") or chat_type

    mentions = resolver.resolve_mentions(event.get("mentions") or [])

    # Short fields first, the message text and the raw event last: if the body
    # is cut anywhere, it is cut in the bulky tail.
    payload: dict[str, Any] = {
        "message_id": message_id,
        "chat_id": chat_id,
        "chat_type": chat_type,
        "sender_open_id": sender_open_id,
        "sender": sender_out,
        "chat": chat_out,
        "mentions": mentions,
    }
    if name_unresolved:
        payload["name_unresolved"] = True
    if reaction_id:
        payload["reaction_id"] = reaction_id
    payload.update(
        {
            "summary": build_summary(text, sender_out, chat_out),
            "console_url": console_url(),
            "instructions": build_instructions(message_id),
            "text": text,
            "raw": event,
        }
    )
    return payload


# A routine webhook hands at most about 4,000 characters of the request body to
# the run. Stay below that in UTF-8 bytes, so the limit holds for CJK text
# whichever way the platform counts. LARK_WEBHOOK_BODY_LIMIT overrides it; 0
# turns the limit off.
WEBHOOK_BODY_LIMIT = 3600


def body_limit() -> int:
    raw = os.environ.get("LARK_WEBHOOK_BODY_LIMIT", "").strip()
    try:
        return int(raw) if raw else WEBHOOK_BODY_LIMIT
    except ValueError:
        return WEBHOOK_BODY_LIMIT


def body_size(payload: dict[str, Any]) -> int:
    """Size of the payload as relay.py posts it."""
    return len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))


def fit_payload(payload: dict[str, Any], limit: int | None = None) -> dict[str, Any]:
    """Make a payload fit the webhook body limit.

    The raw event goes first (it repeats the other fields). If that is not
    enough the text is shortened, text_truncated is set, and the instructions
    say how to read the whole message with lark-cli.
    """
    limit = body_limit() if limit is None else limit
    if limit <= 0 or body_size(payload) <= limit:
        return payload
    fitted = dict(payload)
    fitted.pop("raw", None)
    if body_size(fitted) <= limit:
        return fitted
    text = str(fitted.get("text") or "")
    mid = fitted.get("message_id") or "<message_id>"
    fitted["text_truncated"] = True
    fitted["text_length"] = len(text)
    fitted["instructions"] = (
        str(fitted.get("instructions") or "")
        + "\nThe text in this payload was shortened to fit the webhook (text_truncated). "
        "Read the whole message before you act: "
        f'source "{store_env_path().expanduser().absolute()}" && '
        f"lark-cli im +messages-mget --message-ids {mid} --as bot"
    )
    lo, hi = 0, len(text)
    while lo < hi:  # the longest prefix of the text that still fits
        keep = (lo + hi + 1) // 2
        fitted["text"] = text[:keep]
        if body_size(fitted) <= limit:
            lo = keep
        else:
            hi = keep - 1
    fitted["text"] = text[:lo]
    return fitted
