#!/usr/bin/env python3
"""Tests for relay.py helpers and cliutil.run_cli_retry (no network, nothing real written)."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cliutil  # noqa: E402
import relay  # noqa: E402  (creates logs/, state/, spool/ next to the code; all git-ignored)

WEBHOOK_VARS = ("LARK_BOT_WEBHOOK_URL", "LARK_BOT_WEBHOOK_KEY")


class RelayCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.logs: list[tuple[str, dict]] = []
        for target, value in (
            ("WEBHOOK_ENV", self.dir / "webhook.env"),
            ("SPOOL_PATH", self.dir / "spool" / "pending.jsonl"),
            ("LOG_DIR", self.dir / "logs"),
            ("log", lambda msg, **kw: self.logs.append((msg, kw))),
            ("seen", FakeSeen()),
        ):
            patcher = mock.patch.object(relay, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in WEBHOOK_VARS:
            os.environ.pop(name, None)

    def tearDown(self):
        self.tmp.cleanup()


class FakeSeen:
    def __init__(self):
        self.ids: set[str] = set()

    def contains(self, mid):
        return mid in self.ids

    def add(self, mid):
        self.ids.add(mid)


class LoadWebhookTests(RelayCase):
    def test_nothing_configured(self):
        self.assertIsNone(relay.load_webhook())

    def test_file_only(self):
        relay.WEBHOOK_ENV.write_text("LARK_BOT_WEBHOOK_URL=https://h/x\nLARK_BOT_WEBHOOK_KEY='k1'\n")
        self.assertEqual(relay.load_webhook(), ("https://h/x", "k1"))

    def test_environment_fills_missing_values(self):
        os.environ["LARK_BOT_WEBHOOK_URL"] = " https://env/x "
        os.environ["LARK_BOT_WEBHOOK_KEY"] = " envkey "
        self.assertEqual(relay.load_webhook(), ("https://env/x", "envkey"))
        relay.WEBHOOK_ENV.write_text("LARK_BOT_WEBHOOK_URL=\nLARK_BOT_WEBHOOK_KEY=filekey\n")
        self.assertEqual(relay.load_webhook(), ("https://env/x", "filekey"))
        relay.WEBHOOK_ENV.write_text("LARK_BOT_WEBHOOK_URL=https://file/x\n")
        self.assertEqual(relay.load_webhook(), ("https://file/x", "envkey"))

    def test_incomplete_is_none(self):
        os.environ["LARK_BOT_WEBHOOK_URL"] = "https://env/x"
        self.assertIsNone(relay.load_webhook())

    def test_bearer_prefix_is_stripped_from_both_sources(self):
        for value in ("Bearer abc", "bearer abc", "Authorization: Bearer abc", "authorization: bearer  abc"):
            relay.WEBHOOK_ENV.write_text(f"LARK_BOT_WEBHOOK_URL=https://h/x\nLARK_BOT_WEBHOOK_KEY={value}\n")
            self.assertEqual(relay.load_webhook(), ("https://h/x", "abc"), value)
        relay.WEBHOOK_ENV.unlink()
        os.environ["LARK_BOT_WEBHOOK_URL"] = "https://h/x"
        os.environ["LARK_BOT_WEBHOOK_KEY"] = "Bearer fromenv"
        self.assertEqual(relay.load_webhook(), ("https://h/x", "fromenv"))

    def test_key_starting_with_bearer_letters_is_kept(self):
        relay.WEBHOOK_ENV.write_text("LARK_BOT_WEBHOOK_URL=https://h/x\nLARK_BOT_WEBHOOK_KEY=bearerish\n")
        self.assertEqual(relay.load_webhook(), ("https://h/x", "bearerish"))

    def test_values_are_never_logged(self):
        relay.WEBHOOK_ENV.write_text("LARK_BOT_WEBHOOK_URL=https://h/x\nLARK_BOT_WEBHOOK_KEY=topsecret\n")
        relay.load_webhook()
        self.assertNotIn("topsecret", repr(self.logs))


class EmptyIdGuardTests(RelayCase):
    def setUp(self):
        super().setUp()
        self.bot_events: list[dict] = []
        self.delivered: list[dict] = []
        outer = self

        class FakeLife:
            def handle_possible_bot_event(self, event):
                outer.bot_events.append(event)
                return True

            def start_working(self, mid, chat_id, chat_type):
                return {"reaction_id": "r_1"}

            def mark_webhook_failed(self, mid):
                return None

        for target, value in (
            ("BOT_OPEN_ID", ""),
            ("life", FakeLife()),
            ("resolver", None),
            ("deliver", lambda body: self.delivered.append(body) or True),
        ):
            patcher = mock.patch.object(relay, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_empty_sender_is_not_the_bot_when_bot_id_is_unset(self):
        event = {"message_id": "m1", "chat_id": "c1", "chat_type": "p2p", "sender_id": "", "content": "hi"}
        self.assertEqual(relay.should_forward(event), (True, "p2p"))
        relay.handle_event(event)
        self.assertEqual(self.bot_events, [])
        self.assertEqual(len(self.delivered), 1)

    def test_bot_sender_type_is_still_skipped(self):
        event = {"message_id": "m2", "chat_id": "c1", "chat_type": "p2p", "sender_id": "", "sender_type": "app"}
        relay.handle_event(event)
        self.assertEqual(len(self.bot_events), 1)
        self.assertEqual(self.delivered, [])

    def test_matching_bot_open_id_is_skipped(self):
        with mock.patch.object(relay, "BOT_OPEN_ID", "ou_bot"):
            relay.handle_event({"message_id": "m3", "chat_id": "c1", "chat_type": "p2p", "sender_id": "ou_bot"})
        self.assertEqual(len(self.bot_events), 1)
        self.assertEqual(self.delivered, [])


class SpoolTests(RelayCase):
    def test_concurrent_appends_survive_flushes(self):
        # Every post fails first so flush rewrites the file, then succeeds for the rest.
        posted: list[str] = []

        def post(url, key, body):
            posted.append(body["n"])
            return True

        creds = mock.patch.object(relay, "load_webhook", lambda: ("https://h/x", "k"))
        creds.start()
        self.addCleanup(creds.stop)
        post_patch = mock.patch.object(relay, "post_webhook", post)
        post_patch.start()
        self.addCleanup(post_patch.stop)
        total = 200
        done = threading.Event()

        def writer():
            for n in range(total):
                relay.append_spool({"n": f"m{n}"})
            done.set()

        t = threading.Thread(target=writer)
        t.start()
        while not done.is_set():
            relay.flush_spool()
        t.join()
        relay.flush_spool()
        self.assertEqual(sorted(posted), sorted(f"m{n}" for n in range(total)))
        self.assertEqual(len(posted), total, "an item was posted twice")

    def test_failed_flush_does_not_touch_the_file(self):
        relay.append_spool({"n": "m1"})
        os.utime(relay.SPOOL_PATH, (1_000_000, 1_000_000))
        with mock.patch.object(relay, "load_webhook", lambda: ("https://h/x", "k")), mock.patch.object(
            relay, "post_webhook", lambda *a: False
        ):
            relay.flush_spool()
        self.assertEqual(int(relay.SPOOL_PATH.stat().st_mtime), 1_000_000)
        self.assertIn("m1", relay.SPOOL_PATH.read_text())


class RunCliRetryTests(unittest.TestCase):
    def _run(self, envelope):
        calls = []

        def fake_run_cli(args, timeout=20):
            calls.append(args)
            return envelope

        with mock.patch.object(cliutil, "run_cli", fake_run_cli), mock.patch.object(cliutil.time, "sleep"):
            result = cliutil.run_cli_retry(["im", "x"], attempts=4)
        return result, len(calls)

    def test_no_retry_for_permanent_errors(self):
        for error in (
            {"type": "authorization", "subtype": "app_scope_not_applied"},
            {"type": "authorization"},
            {"type": "validation"},
            {"type": "config"},
            {"subtype": "app_scope_not_applied"},
            {"subtype": "missing_scope"},
            {"subtype": "invalid_argument"},
            {"subtype": "permission_denied"},
        ):
            _, calls = self._run({"ok": False, "error": error})
            self.assertEqual(calls, 1, error)

    def test_retry_for_network_errors(self):
        for error in ({"type": "network", "message": "connection reset"}, {"message": "timeout", "subtype": "timeout"}):
            _, calls = self._run({"ok": False, "error": error})
            self.assertEqual(calls, 4, error)

    def test_success_returns_immediately(self):
        result, calls = self._run({"ok": True, "data": {}})
        self.assertTrue(result["ok"])
        self.assertEqual(calls, 1)



class BusWatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "bus.log"
        self.path.write_text("old: websocket: close 3003: ping timeout\n")

    def add(self, line):
        with self.path.open("a") as f:
            f.write(line + "\n")

    def test_lines_from_before_the_run_do_not_count(self):
        self.assertIsNone(relay.BusWatch(self.path).check())

    def test_events_keep_the_connection(self):
        watch = relay.BusWatch(self.path)
        self.add("Event received: type=im.message.receive_v1 id=1")
        self.assertIsNone(watch.check())

    def test_ping_timeout_asks_for_a_new_connection(self):
        watch = relay.BusWatch(self.path)
        self.add("[SDK] websocket: close 3003: ping timeout")
        self.assertEqual(watch.check(), "bus_connection_trouble")

    def test_silence_asks_for_a_new_connection(self):
        watch = relay.BusWatch(self.path)
        watch.last_activity -= 4000
        self.assertEqual(watch.check(), "idle")
        with mock.patch.dict(os.environ, {"LARK_RELAY_IDLE_RECYCLE_SEC": "0"}):
            self.assertIsNone(watch.check())


if __name__ == "__main__":
    unittest.main()
