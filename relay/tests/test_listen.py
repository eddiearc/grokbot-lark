#!/usr/bin/env python3
"""listen.mjs: the event shape it prints, and reading the secret lark-cli stored (Linux layout)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

RELAY = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")

ENCRYPT = """
import { createCipheriv, randomBytes } from "node:crypto";
import { writeFileSync } from "node:fs";
const [dir, secret] = process.argv.slice(2);
const key = randomBytes(32), iv = randomBytes(12);
const c = createCipheriv("aes-256-gcm", key, iv);
const body = Buffer.concat([c.update(secret, "utf8"), c.final()]);
writeFileSync(dir + "/master.key", key);
writeFileSync(dir + "/appsecret_cli_test.enc", Buffer.concat([iv, body, c.getAuthTag()]));
"""

FLATTEN = """
import { flatten } from %s;
console.log(JSON.stringify(flatten(JSON.parse(process.argv[2]))));
"""


@unittest.skipUnless(NODE, "node is not installed")
class ListenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def node(self, script, *args, env=None):
        path = self.dir / "script.mjs"
        path.write_text(script)
        return subprocess.run([NODE, str(path), *args], capture_output=True, text=True, env=env, timeout=30)

    def flatten(self, data):
        r = self.node(FLATTEN % json.dumps(str(RELAY / "listen.mjs")), json.dumps(data))
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def message(self, **message):
        base = {"message_id": "om_1", "chat_id": "oc_1", "chat_type": "group", "create_time": "1"}
        return {
            "event_id": "e1",
            "sender": {"sender_id": {"open_id": "ou_user"}, "sender_type": "user"},
            "message": {**base, **message},
        }

    def test_text_message_matches_lark_cli(self):
        mentions = [{"key": "@_user_1", "id": {"open_id": "ou_bot"}, "name": "Atlas"}]
        event = self.flatten(
            self.message(message_type="text", content=json.dumps({"text": "@_user_1 hi"}), mentions=mentions, parent_id="om_0")
        )
        self.assertEqual(event["content"], "@Atlas hi")
        self.assertEqual(event["mentions"], [{"key": "@_user_1", "id": "ou_bot", "name": "Atlas"}])
        self.assertEqual((event["message_id"], event["id"], event["sender_id"], event["sender_type"]), ("om_1", "om_1", "ou_user", "user"))
        self.assertEqual(event["reply_to"], "om_0")
        self.assertNotIn("content_pending", event)

    def test_other_types_are_left_for_the_relay_to_render(self):
        event = self.flatten(self.message(message_type="image", content=json.dumps({"image_key": "img"})))
        self.assertEqual((event["content"], event["content_pending"]), ("", True))

    def test_check_reads_the_stored_secret(self):
        config, data = self.dir / "config", self.dir / "data"
        (data / "lark-cli").mkdir(parents=True)
        config.mkdir()
        (config / "config.json").write_text(json.dumps({"apps": [
            {"appId": "cli_test", "brand": "lark", "appSecret": {"source": "keychain", "id": "appsecret:cli_test"}}
        ]}))
        r = self.node(ENCRYPT, str(data / "lark-cli"), "s3cret")
        self.assertEqual(r.returncode, 0, r.stderr)
        env = {"PATH": os.environ["PATH"], "HOME": str(self.dir),
               "LARKSUITE_CLI_CONFIG_DIR": str(config), "LARKSUITE_CLI_DATA_DIR": str(data)}
        if not (RELAY / "node_modules").exists():
            self.skipTest("the node SDK is not installed next to listen.mjs")
        r = subprocess.run([NODE, str(RELAY / "listen.mjs"), "--check"], capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("app_id=cli_test brand=lark", r.stderr)
        self.assertNotIn("s3cret", r.stdout + r.stderr)
        (data / "lark-cli" / "master.key").write_bytes(b"x" * 32)
        r = subprocess.run([NODE, str(RELAY / "listen.mjs"), "--check"], capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 78)


if __name__ == "__main__":
    unittest.main()
