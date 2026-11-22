#!/usr/bin/env python3
"""Unit tests for the relay reaction state machine (synthetic events, no network)."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import identity  # noqa: E402
from identity import (  # noqa: E402
    UNRESOLVED_TTL,
    IdentityResolver,
    build_instructions,
    build_payload,
    build_summary,
    console_url,
)
from lifecycle import ERROR_EMOJI, Lifecycle, _parse_feishu_time  # noqa: E402
from store import Store  # noqa: E402


class FakeOps:
    def __init__(self):
        self.created: list[tuple[str, str]] = []
        self.deleted: list[tuple[str, str]] = []
        self.listed: list[str] = []
        self._seq = 0
        self.by_message: dict[str, dict[str, str]] = {}
        self.fail_create_for: set[str] = set()
        self.fail_first_emojis: set[str] = set()
        self.delete_already_gone: set[str] = set()
        self.drop_on_list: set[str] = set()

    def create(self, message_id: str, emoji_type: str) -> dict:
        if emoji_type in self.fail_first_emojis:
            self.fail_first_emojis.discard(emoji_type)
            return {"ok": False, "error": {"subtype": "invalid_argument", "message": "bad emoji"}}
        if message_id in self.fail_create_for:
            return {"ok": False, "error": {"subtype": "permission_denied", "message": "nope"}}
        self._seq += 1
        rid = f"r_{self._seq}"
        self.created.append((message_id, emoji_type))
        self.by_message.setdefault(message_id, {})[rid] = emoji_type
        return {"ok": True, "data": {"reaction_id": rid, "reaction_type": {"emoji_type": emoji_type}}}

    def delete(self, message_id: str, reaction_id: str) -> dict:
        self.deleted.append((message_id, reaction_id))
        if reaction_id in self.delete_already_gone:
            return {"ok": False, "error": {"subtype": "not_found", "message": "not found"}}
        recs = self.by_message.get(message_id) or {}
        recs.pop(reaction_id, None)
        return {"ok": True, "data": {}}

    def list(self, message_id: str) -> dict:
        self.listed.append(message_id)
        items = []
        if message_id not in self.drop_on_list:
            for rid, emoji in (self.by_message.get(message_id) or {}).items():
                items.append({"reaction_id": rid, "reaction_type": {"emoji_type": emoji}})
        return {"ok": True, "data": {"items": items}}


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class StateMachineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "state.db")
        self.ops = FakeOps()
        self.clock = Clock()
        self.life = Lifecycle(self.store, ops=self.ops, clock=self.clock, log=lambda *a, **k: None)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_start_working_adds_typing(self):
        job = self.life.start_working("msg_1", "chat_1", "p2p")
        self.assertEqual(job["state"], "pending")
        self.assertEqual(job["emoji_type"], "Typing")
        self.assertEqual(job["reaction_id"], "r_1")
        self.assertEqual(self.ops.created, [("msg_1", "Typing")])

    def test_working_emoji_fallback(self):
        self.ops.fail_first_emojis.add("Typing")
        job = self.life.start_working("msg_1", "chat_1", "p2p")
        self.assertEqual(job["emoji_type"], "OnIt")
        self.assertEqual([e for _, e in self.ops.created], ["OnIt"])

    def test_webhook_fail_swaps_to_error(self):
        self.life.start_working("msg_1", "chat_1", "p2p")
        job = self.life.mark_webhook_failed("msg_1")
        self.assertEqual(job["state"], "error")
        self.assertEqual(job["emoji_type"], ERROR_EMOJI)
        self.assertEqual(self.ops.deleted, [("msg_1", "r_1")])
        self.assertEqual(self.ops.created[-1], ("msg_1", "ERROR"))

    def test_timeout_swaps_pending_to_error(self):
        self.life.start_working("msg_1", "chat_1", "p2p")
        self.clock.advance(599)
        self.assertEqual(self.life.mark_timeouts(), [])
        self.assertEqual(self.store.get_job("msg_1")["state"], "pending")
        self.clock.advance(2)
        changed = self.life.mark_timeouts()
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["state"], "error")
        self.assertEqual(changed[0]["emoji_type"], "ERROR")

    def test_bot_reply_to_completes_and_removes(self):
        self.life.start_working("msg_user", "chat_1", "p2p")
        n = self.life.apply_listed_messages(
            "chat_1",
            [
                {
                    "message_id": "msg_bot",
                    "reply_to": "msg_user",
                    "sender": {"id": "example-bot-app-id", "sender_type": "app"},
                    "create_time": "2026-01-02 03:04",
                }
            ],
            "p2p",
        )
        self.assertEqual(n, 1)
        self.assertEqual(self.store.get_job("msg_user")["state"], "done")
        self.assertEqual(self.ops.deleted, [("msg_user", "r_1")])

    def test_event_reply_to_completes(self):
        self.life.start_working("msg_user", "chat_1", "group")
        handled = self.life.handle_possible_bot_event(
            {
                "message_id": "msg_bot",
                "chat_id": "chat_1",
                "chat_type": "group",
                "sender_id": "example-bot-open-id",
                "sender_type": "app",
                "reply_to": "msg_user",
            }
        )
        self.assertTrue(handled)
        self.assertEqual(self.store.get_job("msg_user")["state"], "done")

    def test_p2p_fallback_closes_oldest_pending(self):
        self.life.start_working("msg_old", "chat_1", "p2p")
        self.clock.advance(10)
        self.life.start_working("msg_new", "chat_1", "p2p")
        # Bot message with no reply_to, later than both
        n = self.life.apply_listed_messages(
            "chat_1",
            [
                {
                    "message_id": "msg_bot",
                    "sender": {
                        "id": "example-bot-app-id",
                        "sender_type": "app",
                        "open_bot_id": "example-bot-open-id",
                    },
                    "create_time": 1_000_020,
                }
            ],
            "p2p",
        )
        self.assertEqual(n, 1)
        self.assertEqual(self.store.get_job("msg_old")["state"], "done")
        self.assertEqual(self.store.get_job("msg_new")["state"], "pending")

    def test_p2p_fallback_ignores_older_bot_history(self):
        self.clock.t = 2_000_000
        self.life.start_working("msg_user", "chat_1", "p2p")
        n = self.life.apply_listed_messages(
            "chat_1",
            [
                {
                    "message_id": "msg_old_bot",
                    "sender": {"sender_type": "app", "id": "example-bot-app-id"},
                    "create_time": 1_000_000,  # well before job
                }
            ],
            "p2p",
        )
        self.assertEqual(n, 0)
        self.assertEqual(self.store.get_job("msg_user")["state"], "pending")

    def test_later_reply_removes_error_emoji(self):
        self.life.start_working("msg_user", "chat_1", "p2p")
        self.life.mark_webhook_failed("msg_user")
        err_rid = self.store.get_job("msg_user")["reaction_id"]
        self.life.complete("msg_user", reason="late_reply")
        self.assertEqual(self.store.get_job("msg_user")["state"], "done")
        self.assertIn(("msg_user", err_rid), self.ops.deleted)

    def test_delete_is_idempotent(self):
        self.life.start_working("msg_1", "chat_1", "p2p")
        self.ops.delete_already_gone.add("r_1")
        job = self.life.complete("msg_1")
        self.assertEqual(job["state"], "done")
        again = self.life.complete("msg_1")
        self.assertEqual(again["state"], "done")
        self.assertEqual(self.ops.deleted, [("msg_1", "r_1")])

    def test_reconcile_readds_missing_reaction(self):
        self.life.start_working("msg_1", "chat_1", "p2p")
        self.ops.by_message["msg_1"].clear()
        self.ops.drop_on_list.add("msg_1")
        self.life.reconcile()
        self.assertTrue(any(m == "msg_1" and e == "Typing" for m, e in self.ops.created[1:]))
        job = self.store.get_job("msg_1")
        self.assertEqual(job["state"], "pending")
        self.assertTrue(job["reaction_id"])

    def test_start_working_is_idempotent(self):
        a = self.life.start_working("msg_1", "chat_1", "p2p")
        b = self.life.start_working("msg_1", "chat_1", "p2p")
        self.assertEqual(a["reaction_id"], b["reaction_id"])
        self.assertEqual(len(self.ops.created), 1)


    def test_p2p_fallback_ignores_bot_replies_to_other_messages(self):
        self.life.start_working("msg_a", "chat_1", "p2p")
        self.clock.advance(10)
        self.life.start_working("msg_b", "chat_1", "p2p")
        sender = {"sender_type": "app", "id": "example-bot-app-id"}
        n = self.life.apply_listed_messages(
            "chat_1",
            [
                {"message_id": "bot_1", "reply_to": "msg_a", "sender": sender, "create_time": 1_000_030},
            ],
            "p2p",
        )
        # The reply answers msg_a only; it must not also close msg_b.
        self.assertEqual(n, 1)
        self.assertEqual(self.store.get_job("msg_a")["state"], "done")
        self.assertEqual(self.store.get_job("msg_b")["state"], "pending")
        # A reply to some unrelated message is not a candidate either.
        n = self.life.apply_listed_messages(
            "chat_1",
            [{"message_id": "bot_2", "reply_to": "msg_other", "sender": sender, "create_time": 1_000_040}],
            "p2p",
        )
        self.assertEqual(n, 0)
        self.assertEqual(self.store.get_job("msg_b")["state"], "pending")
        # A plain bot message with no parent still closes the oldest job.
        n = self.life.apply_listed_messages(
            "chat_1",
            [{"message_id": "bot_3", "sender": sender, "create_time": 1_000_050}],
            "p2p",
        )
        self.assertEqual(n, 1)
        self.assertEqual(self.store.get_job("msg_b")["state"], "done")

    def test_abandon_stale_stops_polling_old_jobs(self):
        self.life.start_working("msg_pending", "chat_1", "p2p")
        self.life.start_working("msg_error", "chat_2", "p2p")
        self.life.mark_webhook_failed("msg_error")
        self.clock.advance(6 * 3600 - 1)
        self.life.start_working("msg_fresh", "chat_3", "p2p")
        self.assertEqual(self.life.abandon_stale(), [])
        self.clock.advance(2)
        changed = self.life.abandon_stale()
        self.assertEqual({j["message_id"] for j in changed}, {"msg_pending", "msg_error"})
        for mid in ("msg_pending", "msg_error"):
            job = self.store.get_job(mid)
            self.assertEqual(job["state"], "abandoned")
            self.assertTrue(job["reaction_id"])  # the reaction stays on the message
        self.assertEqual([j["message_id"] for j in self.store.active_jobs()], ["msg_fresh"])
        self.assertEqual([j["message_id"] for j in self.store.pending_jobs()], ["msg_fresh"])
        self.assertEqual(self.store.active_in_chat("chat_1"), [])
        self.assertEqual(self.life.abandon_stale(), [])
        # A reply that shows up later still completes the job and removes the emoji.
        rid = self.store.get_job("msg_pending")["reaction_id"]
        self.life.complete("msg_pending", reason="late_reply")
        self.assertEqual(self.store.get_job("msg_pending")["state"], "done")
        self.assertIn(("msg_pending", rid), self.ops.deleted)


class FeishuTimeTests(unittest.TestCase):
    def test_naive_string_is_local_time(self):
        expected = time.mktime((2026, 1, 2, 3, 4, 0, 0, 0, -1))
        self.assertEqual(_parse_feishu_time("2026-01-02 03:04"), expected)
        self.assertEqual(_parse_feishu_time("2026-01-02 03:04:30"), expected + 30)
        self.assertEqual(
            _parse_feishu_time("2026-01-02 03:04"), datetime(2026, 1, 2, 3, 4).astimezone().timestamp()
        )

    def test_numbers_are_epoch(self):
        self.assertEqual(_parse_feishu_time(1_700_000_000), 1_700_000_000)
        self.assertEqual(_parse_feishu_time("1700000000000"), 1_700_000_000)
        self.assertIsNone(_parse_feishu_time("nonsense"))


class FakeCli:
    def __init__(self, envelope):
        self.envelope = envelope
        self.calls = 0

    def __call__(self, args, **kwargs):
        self.calls += 1
        return self.envelope


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "state.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_unresolved_user_is_cached_briefly(self):
        fake = FakeCli({"ok": False, "error": {"type": "authorization", "subtype": "app_scope_not_applied"}})
        resolver = IdentityResolver(self.store)
        with mock.patch.object(identity, "run_cli_retry", fake):
            profile = resolver.resolve_user("ou_1")
        self.assertTrue(profile["name_unresolved"])
        self.assertIn("scope_hint", profile)
        now = time.time()
        self.assertIsNotNone(self.store.cache_get("user", "ou_1", now=now + UNRESOLVED_TTL - 5))
        self.assertIsNone(self.store.cache_get("user", "ou_1", now=now + UNRESOLVED_TTL + 5))

    def test_resolved_user_keeps_long_ttl(self):
        fake = FakeCli({"ok": True, "data": {"user": {"name": "Alice", "open_id": "ou_2"}}})
        resolver = IdentityResolver(self.store)
        with mock.patch.object(identity, "run_cli_retry", fake):
            profile = resolver.resolve_user("ou_2")
        self.assertEqual(profile["name"], "Alice")
        self.assertFalse(profile["name_unresolved"])
        self.assertIsNotNone(self.store.cache_get("user", "ou_2", now=time.time() + 3600))


class ConsoleUrlTests(unittest.TestCase):
    def _env(self, **values):
        base = {k: "" for k in ("LARK_CONSOLE_URL", "LARK_BRAND", "LARK_BOT_APP_ID")}
        return mock.patch.dict(os.environ, {**base, **values})

    def test_without_app_id(self):
        with self._env():
            self.assertEqual(console_url(), "https://open.feishu.cn/app")
        with self._env(LARK_BRAND="lark"):
            self.assertEqual(console_url(), "https://open.larksuite.com/app")

    def test_with_app_id_points_at_permission_page(self):
        with self._env(LARK_BOT_APP_ID="cli_abc"):
            self.assertEqual(console_url(), "https://open.feishu.cn/app/cli_abc/auth")
        with self._env(LARK_BOT_APP_ID="cli_abc", LARK_BRAND="larksuite"):
            self.assertEqual(console_url(), "https://open.larksuite.com/app/cli_abc/auth")

    def test_explicit_url_wins(self):
        with self._env(LARK_CONSOLE_URL="https://console.example/x", LARK_BOT_APP_ID="cli_abc"):
            self.assertEqual(console_url(), "https://console.example/x")


class PayloadTests(unittest.TestCase):
    def test_instructions_are_english_and_have_no_owner(self):
        text = build_instructions("msg_abc")
        self.assertIn("forwarded by the grokbot-lark relay", text)
        self.assertIn("lark-cli im +messages-reply --message-id msg_abc --as bot", text)
        self.assertIn("source", text)
        self.assertIn("lark-bot/env.sh", text)
        self.assertNotIn("lark-bot-cli", text)
        self.assertIn("webhook.env", text)
        self.assertIn("console_url", text)
        self.assertIn("publish", text.lower())
        self.assertIn("sender's language", text)
        self.assertNotIn("is_owner", text)
        self.assertNotIn("owner", text.lower().replace("no sender is a special owner", ""))

    def test_instructions_use_the_relays_store_path(self):
        with mock.patch.dict(os.environ, {"LARK_BOT_ENV": "/srv/bots/second/env.sh"}):
            text = build_instructions("msg_abc")
        self.assertIn('source "/srv/bots/second/env.sh" && lark-cli im +messages-reply', text)
        self.assertNotIn("lark-bot/env.sh", text)
        self.assertIn("app_scope_not_applied", text)

    def test_payload_has_console_url(self):
        class R:
            def resolve_user(self, open_id, message_id=None, hint=None):
                return {"open_id": open_id, "name": "A", "name_unresolved": False}

            def resolve_chat(self, chat_id, chat_type=None):
                return {"chat_id": chat_id, "type": chat_type}

            def resolve_mentions(self, mentions):
                return []

        env = {"LARK_CONSOLE_URL": "", "LARK_BRAND": "", "LARK_BOT_APP_ID": "cli_abc"}
        with mock.patch.dict(os.environ, env):
            payload = build_payload({"message_id": "m", "chat_id": "c", "chat_type": "p2p", "sender_id": "u"}, R())
        self.assertEqual(payload["console_url"], "https://open.feishu.cn/app/cli_abc/auth")

    def test_summary_has_no_owner_label(self):
        s = build_summary(
            "hello",
            {"name": "Alice", "open_id": "user_example"},
            {"type": "p2p", "name": "Direct message"},
        )
        self.assertIn("Alice", s)
        self.assertIn("direct message", s.lower())
        self.assertIn("hello", s)
        self.assertNotIn("is_owner", s)

    def test_payload_has_no_is_owner(self):
        class FakeResolver:
            def resolve_user(self, open_id, message_id=None, hint=None):
                return {
                    "open_id": open_id,
                    "union_id": "union_example",
                    "name": "Alice",
                    "name_unresolved": False,
                }

            def resolve_chat(self, chat_id, chat_type=None):
                return {"chat_id": chat_id, "name": "Direct message", "type": chat_type or "p2p"}

            def resolve_mentions(self, mentions):
                return [{"open_id": m.get("id"), "name": m.get("name")} for m in mentions or []]

        event = {
            "message_id": "msg_1",
            "chat_id": "chat_1",
            "chat_type": "p2p",
            "sender_id": "user_example",
            "content": "hi",
            "mentions": [],
        }
        payload = build_payload(event, FakeResolver(), reaction_id="r_9")
        self.assertNotIn("is_owner", payload)
        self.assertNotIn("is_owner", payload["sender"])
        self.assertEqual(payload["sender"]["name"], "Alice")
        self.assertEqual(payload["sender"]["open_id"], "user_example")
        self.assertEqual(payload["sender"]["union_id"], "union_example")
        self.assertEqual(payload["chat"]["type"], "p2p")
        self.assertEqual(payload["reaction_id"], "r_9")
        self.assertIn("instructions", payload)
        self.assertIn("summary", payload)
        blob = str(payload)
        self.assertNotIn("is_owner", blob)


if __name__ == "__main__":
    unittest.main()
