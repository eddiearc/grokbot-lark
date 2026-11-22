#!/usr/bin/env python3
"""Working/error reaction lifecycle managed entirely by the relay."""

from __future__ import annotations

import os
import time
from typing import Any, Callable, Protocol

from cliutil import find_field, run_cli_retry
from store import Store

WORKING_EMOJIS = ("Typing", "OnIt", "OK")
ERROR_EMOJI = "ERROR"
TIMEOUT_SECONDS = 10 * 60
POLL_INTERVAL = 20
BOT_OPEN_ID = os.environ.get("LARK_BOT_OPEN_ID", "").strip()
BOT_APP_ID = os.environ.get("LARK_BOT_APP_ID", "").strip()

LogFn = Callable[..., None]


class ReactionOps(Protocol):
    def create(self, message_id: str, emoji_type: str) -> dict[str, Any]: ...
    def delete(self, message_id: str, reaction_id: str) -> dict[str, Any]: ...
    def list(self, message_id: str) -> dict[str, Any]: ...


class CliReactionOps:
    def __init__(self, log: LogFn | None = None):
        self.log = log

    def create(self, message_id: str, emoji_type: str) -> dict[str, Any]:
        return run_cli_retry(
            [
                "im",
                "reactions",
                "create",
                "--params",
                f'{{"message_id":"{message_id}"}}',
                "--data",
                f'{{"reaction_type":{{"emoji_type":"{emoji_type}"}}}}',
                "--json",
            ],
            timeout=20,
            log=self.log,
            op="reaction_create",
        )

    def delete(self, message_id: str, reaction_id: str) -> dict[str, Any]:
        return run_cli_retry(
            [
                "im",
                "reactions",
                "delete",
                "--params",
                f'{{"message_id":"{message_id}","reaction_id":"{reaction_id}"}}',
                "--json",
            ],
            timeout=20,
            log=self.log,
            op="reaction_delete",
        )

    def list(self, message_id: str) -> dict[str, Any]:
        return run_cli_retry(
            [
                "im",
                "reactions",
                "list",
                "--params",
                f'{{"message_id":"{message_id}"}}',
                "--json",
            ],
            timeout=20,
            log=self.log,
            op="reaction_list",
        )


def _is_bot_sender(sender: dict[str, Any] | None, sender_id: str | None = None, sender_type: str | None = None) -> bool:
    sender = sender or {}
    sid = sender_id or sender.get("id") or sender.get("open_bot_id") or sender.get("sender_id")
    stype = (sender_type or sender.get("sender_type") or "").lower()
    if stype in {"app", "bot"}:
        return True
    known = {x for x in (BOT_OPEN_ID, BOT_APP_ID) if x}
    return bool(sid) and sid in known


def _extract_reaction_id(envelope: dict[str, Any]) -> str | None:
    rid = find_field(envelope, "reaction_id")
    if isinstance(rid, str) and rid:
        return rid
    return None


def _extract_reactions(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    data = envelope.get("data") or envelope
    items = None
    if isinstance(data, dict):
        items = data.get("items") or data.get("reactions") or data.get("data")
    if items is None:
        items = find_field(envelope, "items")
    if isinstance(items, list):
        return [x for x in items if isinstance(x, dict)]
    return []


class Lifecycle:
    def __init__(
        self,
        store: Store,
        ops: ReactionOps | None = None,
        log: LogFn | None = None,
        clock: Callable[[], float] | None = None,
        working_emojis: tuple[str, ...] = WORKING_EMOJIS,
        error_emoji: str = ERROR_EMOJI,
        timeout_seconds: float = TIMEOUT_SECONDS,
    ):
        self.store = store
        self.ops = ops or CliReactionOps(log=log)
        self.log = log or (lambda *a, **k: None)
        self.clock = clock or time.time
        self.working_emojis = working_emojis
        self.error_emoji = error_emoji
        self.timeout_seconds = timeout_seconds
        self.chosen_working: str | None = None

    def start_working(self, message_id: str, chat_id: str, chat_type: str | None = None) -> dict[str, Any]:
        job = self.store.get_job(message_id)
        if job and job.get("state") in {"pending", "error"} and job.get("reaction_id"):
            return job
        emoji, rid = self._add_emoji(message_id, self.working_emojis)
        now = self.clock()
        return self.store.upsert_job(
            message_id=message_id,
            chat_id=chat_id,
            chat_type=chat_type,
            reaction_id=rid,
            emoji_type=emoji,
            state="pending",
            created_at=now,
            now=now,
        )

    def mark_webhook_failed(self, message_id: str) -> dict[str, Any] | None:
        job = self.store.get_job(message_id)
        if not job:
            return None
        return self._swap_to_error(job)

    def mark_timeouts(self) -> list[dict[str, Any]]:
        now = self.clock()
        changed = []
        for job in self.store.pending_jobs():
            created = float(job.get("created_at") or 0)
            if created and (now - created) >= self.timeout_seconds:
                updated = self._swap_to_error(job)
                if updated:
                    changed.append(updated)
                    self.log("reaction_timeout", message_id=job["message_id"])
        return changed

    def complete(self, message_id: str, reason: str = "reply") -> dict[str, Any] | None:
        job = self.store.get_job(message_id)
        if not job:
            return None
        if job.get("state") == "done":
            return job
        self._delete_emoji(job)
        now = self.clock()
        updated = self.store.update_job(message_id, now=now, state="done", reaction_id=None)
        self.log("reaction_complete", message_id=message_id, reason=reason)
        return updated

    def handle_possible_bot_event(self, event: dict[str, Any]) -> bool:
        """If this inbound event is a bot reply to a pending job, complete it. Return True if handled as bot."""
        sender_id = event.get("sender_id") or (event.get("sender") or {}).get("id")
        sender_type = event.get("sender_type") or (event.get("sender") or {}).get("sender_type")
        if not _is_bot_sender(event.get("sender") if isinstance(event.get("sender"), dict) else None, sender_id, sender_type):
            return False
        parent = event.get("reply_to") or event.get("parent_id") or event.get("root_id")
        if parent and self.store.get_job(parent) and self.store.get_job(parent).get("state") in {"pending", "error"}:
            self.complete(parent, reason="event_reply_to")
            return True
        # p2p fallback: any later bot message in that chat closes oldest active job
        chat_id = event.get("chat_id")
        chat_type = event.get("chat_type") or ""
        if chat_id and chat_type == "p2p":
            self._complete_oldest_p2p(chat_id, event_time=self.clock(), reason="event_p2p_fallback")
        return True

    def apply_listed_messages(self, chat_id: str, messages: list[dict[str, Any]], chat_type: str | None = None) -> int:
        """Scan +chat-messages-list results and complete jobs that have a later bot reply."""
        active = self.store.active_in_chat(chat_id)
        if not active:
            return 0
        by_id = {j["message_id"]: j for j in active}
        completed = 0
        bot_msgs: list[dict[str, Any]] = []
        for msg in messages:
            sender = msg.get("sender") or {}
            if not _is_bot_sender(sender if isinstance(sender, dict) else None):
                continue
            bot_msgs.append(msg)
            parent = msg.get("reply_to") or msg.get("parent_id") or msg.get("root_id")
            if parent and parent in by_id:
                self.complete(parent, reason="poll_reply_to")
                completed += 1
                by_id.pop(parent, None)
        if (chat_type or (active[0].get("chat_type") if active else "")) == "p2p" and by_id:
            # Any later bot message in p2p closes the oldest remaining job.
            # Ignore bot history that predates the job (use message_position if present).
            remaining = [by_id[j["message_id"]] for j in active if j["message_id"] in by_id]
            remaining.sort(key=lambda j: float(j.get("created_at") or 0))
            used = set()
            for job in remaining:
                later = self._later_bot_message(job, bot_msgs, used)
                if later:
                    used.add(id(later))
                    self.complete(job["message_id"], reason="poll_p2p_fallback")
                    completed += 1
        return completed

    def reconcile(self) -> None:
        for job in self.store.active_jobs():
            mid = job["message_id"]
            rid = job.get("reaction_id")
            wanted = job.get("emoji_type") or (
                self.error_emoji if job.get("state") == "error" else (self.chosen_working or self.working_emojis[0])
            )
            present = False
            if rid:
                listed = self.ops.list(mid)
                if listed.get("ok"):
                    for item in _extract_reactions(listed):
                        if item.get("reaction_id") == rid:
                            present = True
                            break
                # If list failed, leave as-is rather than double-adding.
                elif listed.get("error"):
                    self.log("reconcile_list_failed", message_id=mid)
                    continue
            if present:
                continue
            # Re-add missing reaction for active jobs.
            emoji, new_rid = self._add_emoji(
                mid,
                (wanted,) if wanted else self.working_emojis,
                fallback=self.working_emojis if job.get("state") != "error" else (self.error_emoji,),
            )
            self.store.update_job(mid, now=self.clock(), reaction_id=new_rid, emoji_type=emoji)
            self.log("reaction_reconciled", message_id=mid, emoji_type=emoji)

    def _later_bot_message(
        self, job: dict[str, Any], bot_msgs: list[dict[str, Any]], used: set[int]
    ) -> dict[str, Any] | None:
        job_pos = None
        # We don't always have the original message in the list; use created_at slack of 90s
        # because list create_time is minute-precision.
        created = float(job.get("created_at") or 0)
        slack = 90
        for msg in bot_msgs:
            if id(msg) in used:
                continue
            # Prefer message_position if we can compare; otherwise time.
            ct = msg.get("create_time")
            ts = _parse_feishu_time(ct)
            if ts is not None and created and ts + slack < created:
                continue
            return msg
        return None

    def _complete_oldest_p2p(self, chat_id: str, event_time: float, reason: str) -> None:
        jobs = self.store.active_in_chat(chat_id)
        if not jobs:
            return
        # Only close a job that started at or before this event (ignore future).
        eligible = [j for j in jobs if float(j.get("created_at") or 0) <= event_time + 1]
        if not eligible:
            return
        self.complete(eligible[0]["message_id"], reason=reason)

    def _add_emoji(
        self,
        message_id: str,
        candidates: tuple[str, ...] | list[str],
        fallback: tuple[str, ...] | None = None,
    ) -> tuple[str | None, str | None]:
        tried = []
        for emoji in list(candidates) + list(fallback or ()):
            if emoji in tried:
                continue
            tried.append(emoji)
            result = self.ops.create(message_id, emoji)
            if result.get("ok"):
                rid = _extract_reaction_id(result)
                if emoji in self.working_emojis:
                    self.chosen_working = emoji
                self.log("reaction_added", message_id=message_id, emoji_type=emoji, reaction_id=rid)
                return emoji, rid
            err = result.get("error") or {}
            self.log("reaction_add_failed", message_id=message_id, emoji_type=emoji, error=err.get("subtype") or err.get("message"))
        return None, None

    def _delete_emoji(self, job: dict[str, Any]) -> None:
        mid = job["message_id"]
        rid = job.get("reaction_id")
        if not rid:
            return
        result = self.ops.delete(mid, rid)
        if result.get("ok"):
            self.log("reaction_removed", message_id=mid, reaction_id=rid)
            return
        err = result.get("error") or {}
        # Already gone is success (idempotent).
        msg = str(err.get("message") or "") + str(err.get("subtype") or "")
        if any(s in msg.lower() for s in ("not found", "not_found", "already", "2300")):
            self.log("reaction_removed", message_id=mid, reaction_id=rid, note="already_gone")
            return
        self.log("reaction_remove_failed", message_id=mid, reaction_id=rid, error=err.get("subtype") or err.get("message"))

    def _swap_to_error(self, job: dict[str, Any]) -> dict[str, Any] | None:
        mid = job["message_id"]
        if job.get("state") == "done":
            return job
        if job.get("state") == "error" and job.get("emoji_type") == self.error_emoji and job.get("reaction_id"):
            return job
        self._delete_emoji(job)
        emoji, rid = self._add_emoji(mid, (self.error_emoji,))
        now = self.clock()
        updated = self.store.update_job(
            mid,
            now=now,
            state="error",
            emoji_type=emoji or self.error_emoji,
            reaction_id=rid,
            error_at=now,
        )
        self.log("reaction_error", message_id=mid, emoji_type=emoji)
        return updated


def _parse_feishu_time(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # milliseconds vs seconds
        v = float(value)
        if v > 1e12:
            return v / 1000.0
        return v
    if isinstance(value, str) and value.isdigit():
        return _parse_feishu_time(int(value))
    if isinstance(value, str):
        from datetime import datetime, timezone

        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                dt = datetime.strptime(value, fmt)
                # The list API often returns naive timestamps; treat them as UTC.
                return dt.replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                continue
    return None
