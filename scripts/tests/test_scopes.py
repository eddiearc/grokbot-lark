from __future__ import annotations

import json
import subprocess
import sys
import unittest
from urllib.parse import parse_qs, urlparse

import support  # noqa: F401  (puts scripts/ on sys.path)
import scope_link


class AddonsTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        addons = scope_link.build_addons(["a:b", "c:d:readonly"])
        text = scope_link.encode_addons(addons)
        self.assertEqual(scope_link.decode_addons(text), addons)
        self.assertEqual(
            addons,
            {
                "scopes": {"tenant": ["a:b", "c:d:readonly"], "user": []},
                "events": {"items": {"tenant": ["im.message.receive_v1"], "user": []}},
            },
        )

    def test_url_safe_alphabet_without_padding(self) -> None:
        scopes = scope_link.load_scopes(scope_link.DEFAULT_SCOPES)
        text = scope_link.encode_addons(scope_link.build_addons(scopes))
        self.assertNotIn("=", text)
        self.assertNotIn("+", text)
        self.assertNotIn("/", text)
        self.assertRegex(text, r"^[A-Za-z0-9_-]+$")
        # deterministic: gzip mtime is pinned
        self.assertEqual(text, scope_link.encode_addons(scope_link.build_addons(scopes)))

    def test_custom_events(self) -> None:
        addons = scope_link.build_addons(["x:y"], events=("a.b", "c.d"))
        self.assertEqual(addons["events"]["items"]["tenant"], ["a.b", "c.d"])

    def test_launcher_url(self) -> None:
        addons = scope_link.build_addons(["x:y"])
        url = scope_link.launcher_url("cli_abc", "feishu", addons)
        parsed = urlparse(url)
        self.assertEqual(f"{parsed.scheme}://{parsed.netloc}{parsed.path}", "https://open.feishu.cn/page/launcher")
        query = parse_qs(parsed.query)
        self.assertEqual(query["clientID"], ["cli_abc"])
        self.assertEqual(scope_link.decode_addons(query["addons"][0]), addons)
        self.assertTrue(
            scope_link.launcher_url("cli_abc", "lark", addons).startswith(
                "https://open.larksuite.com/page/launcher?clientID=cli_abc&addons="
            )
        )


class BrandTests(unittest.TestCase):
    def test_aliases(self) -> None:
        for value, expected in [
            ("feishu", "feishu"),
            ("lark", "lark"),
            ("larksuite", "lark"),
            ("international", "lark"),
            ("LarkSuite", "lark"),
        ]:
            self.assertEqual(scope_link.normalize_brand(value), expected)

    def test_rejects_unknown(self) -> None:
        for value in ("", "wechat", "feishu2", "lark.com"):
            with self.assertRaises(ValueError):
                scope_link.normalize_brand(value)

    def test_open_host(self) -> None:
        self.assertEqual(scope_link.open_host("feishu"), "https://open.feishu.cn")
        self.assertEqual(scope_link.open_host("international"), "https://open.larksuite.com")


class ScopeFileTests(unittest.TestCase):
    def test_txt_and_json_match(self) -> None:
        txt = scope_link.load_scopes(scope_link.DEFAULT_SCOPES)
        data = json.loads((scope_link.ROOT / "scopes" / "feishu-scopes-import.json").read_text())
        listed = data["scopes"]["tenant"]
        self.assertEqual(len(txt), len(set(txt)), "duplicates in bot-scopes.txt")
        self.assertEqual(len(listed), len(set(listed)), "duplicates in feishu-scopes-import.json")
        self.assertEqual(set(txt), set(listed))
        self.assertIn("application:application:self_manage", txt)


class CliTests(unittest.TestCase):
    def run_cli(self, *args: str) -> str:
        return subprocess.run(
            [sys.executable, str(scope_link.ROOT / "scripts" / "scope_link.py")] + list(args),
            check=True,
            stdout=subprocess.PIPE,
            universal_newlines=True,
        ).stdout.strip()

    def test_classic_default(self) -> None:
        url = self.run_cli("--app-id", "cli_test", "--brand", "feishu")
        self.assertTrue(url.startswith("https://open.feishu.cn/page/scope-apply?clientID=cli_test&scopes="))
        scopes = parse_qs(urlparse(url).query)["scopes"][0].split(",")
        self.assertEqual(scopes, scope_link.load_scopes(scope_link.DEFAULT_SCOPES))

    def test_alias_and_launcher(self) -> None:
        url = self.run_cli("--app-id", "cli_test", "--brand", "international", "--launcher")
        self.assertTrue(url.startswith("https://open.larksuite.com/page/launcher?clientID=cli_test&addons="))
        addons = scope_link.decode_addons(parse_qs(urlparse(url).query)["addons"][0])
        self.assertEqual(addons["scopes"]["tenant"], scope_link.load_scopes(scope_link.DEFAULT_SCOPES))

    def test_bad_brand_rejected(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(scope_link.ROOT / "scripts" / "scope_link.py"), "--app-id", "x", "--brand", "nope"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
