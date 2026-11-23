from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import support
from support import FAKE_URL_QUERY, OnboardCase, kv, lines_for
import scope_link

EVENT = "im.message.receive_v1"
REQUIRED = scope_link.load_scopes(scope_link.DEFAULT_SCOPES)
FAKE_BASE = "https://open.feishu.cn/page/cli?" + FAKE_URL_QUERY
# onboard.py moves lark-cli's link to the launcher page, which honors name, avatar and addons.
LAUNCHER_BASE = "https://open.feishu.cn/page/launcher?" + FAKE_URL_QUERY + "&tp=lark_cli_template"


class AppTests(OnboardCase):
    def test_new_app_output_and_store(self) -> None:
        proc = self.run_onboard(["app", "--bot", "t1"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = kv(proc.stdout)
        self.assertEqual(out["status"], "waiting_for_scan")
        self.assertEqual(out["bot"], "t1")
        self.assertEqual(out["app_name"], "t1")
        self.assertEqual(out["qr_carries"], f"app name, {len(REQUIRED)} bot scopes, {EVENT}")
        self.assertIn("python3 scripts/onboard.py wait --bot t1", out["next"])
        self.assertTrue(Path(out["qr_png"]).is_file())

        store = self.store("t1")
        for d in (store, store / "config", store / "data", store / "logs"):
            self.assertEqual(self.mode(d), 0o700, d)
        env_sh = store / "env.sh"
        self.assertEqual(self.mode(env_sh), 0o600)
        text = env_sh.read_text()
        self.assertIn("no secrets", text)
        self.assertIn('export PATH="${HOME}/.local/bin:${PATH}"', text)
        self.assertIn(f'export LARKSUITE_CLI_CONFIG_DIR="{store / "config"}"', text)
        self.assertIn(f'export LARKSUITE_CLI_DATA_DIR="{store / "data"}"', text)
        self.assertIn(f'export LARKSUITE_CLI_LOG_DIR="{store / "logs"}"', text)
        state = json.loads((store / "app-init.json").read_text())
        for key in ("pid", "url", "full_url", "qr_png", "out_log", "err_log", "started_at", "name"):
            self.assertIn(key, state)
        self.assertEqual(state["url"], out["verification_url"])
        self.assertNotEqual(state["full_url"], state["url"])

    def test_output_lines_and_order(self) -> None:
        proc = self.run_onboard(["app", "--bot", "t1"])
        keys = [line.split(": ", 1)[0] for line in proc.stdout.splitlines()]
        self.assertEqual(
            keys,
            ["status", "bot", "app_name", "qr_png", "qr_carries", "verification_url", "next"],
        )
        self.assertNotIn("scopes_preset", proc.stdout)
        nxt = kv(proc.stdout)["next"]
        self.assertTrue(nxt.startswith("Send the operator ONE message now: attach the QR image"), nxt)
        self.assertIn("paste verification_url exactly as printed", nxt)

    def test_printed_url_is_short_and_has_no_scopes(self) -> None:
        out = kv(self.run_onboard(["app", "--bot", "t1"]).stdout)
        url = out["verification_url"]
        self.assertTrue(url.startswith(LAUNCHER_BASE + "&"), url)
        self.assertLess(len(url), 400)
        self.assertNotIn("addons=", url)
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["user_code"], ["ABCD-EFGH"])
        self.assertEqual(query["name"], ["t1"])
        self.assertEqual(query["desc"], ["Grok Bot"])
        self.assertEqual(query["createOnly"], ["true"])
        self.assertTrue(url.endswith("createOnly=true"))

    def test_qr_carries_the_full_url(self) -> None:
        out = kv(self.run_onboard(["app", "--bot", "t1"]).stdout)
        short = out["verification_url"]
        qr_calls = self.calls_matching("auth", "qrcode")
        self.assertEqual(len(qr_calls), 1)
        full = qr_calls[0]["argv"][2]
        self.assertTrue(full.startswith(LAUNCHER_BASE + "&"), full)
        self.assertTrue(full.endswith("&createOnly=true"), full)
        self.assertNotEqual(full, short)
        query = parse_qs(urlparse(full).query)
        self.assertEqual(query["name"], ["t1"])
        self.assertEqual(query["desc"], ["Grok Bot"])
        self.assertEqual(query["createOnly"], ["true"])
        addons = scope_link.decode_addons(query["addons"][0])
        self.assertEqual(addons["scopes"]["tenant"], REQUIRED)
        self.assertEqual(addons["scopes"]["user"], [])
        self.assertEqual(addons["events"]["items"]["tenant"], [EVENT])
        # the short link is the full one minus addons
        raw_addons = full.split("&addons=")[1].split("&")[0]
        self.assertEqual(full.replace("&addons=" + raw_addons, ""), short)

    def test_every_cli_call_uses_the_bot_store(self) -> None:
        self.run_onboard(["app", "--bot", "t1"])
        store = self.store("t1")
        calls = self.calls()
        self.assertTrue(calls)
        for rec in calls:
            self.assertEqual(rec["config_dir"], str(store / "config"))
            self.assertEqual(rec["data_dir"], str(store / "data"))
            self.assertEqual(rec["log_dir"], str(store / "logs"))
        init = self.calls_matching("config", "init", "--new")
        self.assertEqual(len(init), 1)
        self.assertEqual(init[0]["argv"], ["config", "init", "--new", "--brand", "feishu"])

    def test_qr_uses_relative_output_in_tmp(self) -> None:
        self.run_onboard(["app", "--bot", "t1"])
        qr = self.calls_matching("auth", "qrcode")[0]
        self.assertIn("&addons=", qr["argv"][2])
        self.assertEqual(qr["argv"][3:5], ["--output", "app-qr.png"])
        self.assertIn("grokbot-lark-", qr["cwd"])
        self.assertEqual(qr["argv"][5:], ["--size", "1024"])  # long URL

    def test_name_and_desc_are_percent_encoded(self) -> None:
        proc = self.run_onboard(
            ["app", "--bot", "t1", "--name", "Grok Bot 机器人", "--desc", "a&b c",
             "--avatar", "https://img.example/a.png?x=1&y=2", "--avatar", "https://img.example/b.png"]
        )
        url = kv(proc.stdout)["verification_url"]
        self.assertIn("&name=Grok%20Bot%20%E6%9C%BA%E5%99%A8%E4%BA%BA&", url)
        self.assertIn("&desc=a%26b%20c&", url)
        self.assertIn("&avatar=https%3A%2F%2Fimg.example%2Fa.png%3Fx%3D1%26y%3D2&", url)
        self.assertIn("&avatar=https%3A%2F%2Fimg.example%2Fb.png&", url)
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["name"], ["Grok Bot 机器人"])
        self.assertEqual(kv(proc.stdout)["app_name"], "Grok Bot 机器人")

    def test_no_scopes_omits_addons(self) -> None:
        proc = self.run_onboard(["app", "--bot", "t1", "--no-scopes"])
        out = kv(proc.stdout)
        self.assertNotIn("addons=", out["verification_url"])
        self.assertEqual(out["qr_carries"], "app name")
        self.assertIn("createOnly=true", out["verification_url"])
        qr = self.calls_matching("auth", "qrcode")[0]
        self.assertEqual(qr["argv"][2], out["verification_url"])
        state = json.loads((self.store("t1") / "app-init.json").read_text())
        self.assertEqual(state["url"], state["full_url"])
        self.assertEqual(qr["argv"][-2:], ["--size", "512"])  # short URL

    def test_qr_failure_is_not_fatal(self) -> None:
        self.flag("qr-fail")
        proc = self.run_onboard(["app", "--bot", "t1"])
        self.assertEqual(proc.returncode, 0)
        out = kv(proc.stdout)
        self.assertEqual(out["status"], "waiting_for_scan")
        self.assertTrue(out["qr_png"].startswith("unavailable ("), out["qr_png"])

    def test_second_run_while_pending_starts_no_second_process(self) -> None:
        first = self.run_onboard(["app", "--bot", "t1"])
        pid1 = json.loads((self.store("t1") / "app-init.json").read_text())["pid"]
        second = self.run_onboard(["app", "--bot", "t1"])
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        a, b = kv(first.stdout), kv(second.stdout)
        self.assertEqual(b["status"], "waiting_for_scan")
        self.assertEqual(a["verification_url"], b["verification_url"])
        self.assertEqual(len(self.calls_matching("config", "init", "--new")), 1)
        self.assertEqual(pid1, json.loads((self.store("t1") / "app-init.json").read_text())["pid"])
        self.assertEqual(a["qr_carries"], b["qr_carries"])

    def test_qr_is_regenerated_from_the_full_url(self) -> None:
        first = kv(self.run_onboard(["app", "--bot", "t1"]).stdout)
        full = self.calls_matching("auth", "qrcode")[0]["argv"][2]
        Path(first["qr_png"]).unlink()
        second = self.run_onboard(["app", "--bot", "t1"])
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        out = kv(second.stdout)
        self.assertEqual(out["verification_url"], first["verification_url"])
        self.assertTrue(Path(out["qr_png"]).is_file())
        qr_calls = self.calls_matching("auth", "qrcode")
        self.assertEqual(len(qr_calls), 2)
        self.assertEqual(qr_calls[1]["argv"][2], full)

    def test_regeneration_falls_back_to_url_for_an_old_state_file(self) -> None:
        first = kv(self.run_onboard(["app", "--bot", "t1"]).stdout)
        path = self.store("t1") / "app-init.json"
        state = json.loads(path.read_text())
        del state["full_url"]
        path.write_text(json.dumps(state))
        Path(first["qr_png"]).unlink()
        out = kv(self.run_onboard(["app", "--bot", "t1"]).stdout)
        self.assertTrue(Path(out["qr_png"]).is_file())
        self.assertEqual(self.calls_matching("auth", "qrcode")[1]["argv"][2], first["verification_url"])

    def test_registration_is_detached_and_logged(self) -> None:
        self.run_onboard(["app", "--bot", "t1"])
        logs = self.store("t1") / "logs"
        self.assertIn("user_code=ABCD-EFGH", (logs / "app-init.err").read_text())
        self.assertFalse((logs / "app-init.rc").exists())
        self.flag("scan-done")
        for _ in range(100):
            if (logs / "app-init.rc").exists():
                break
            import time
            time.sleep(0.1)
        self.assertEqual((logs / "app-init.rc").read_text().strip(), "0")

    def test_invalid_bot_name_and_brand(self) -> None:
        for bad in ("../evil", "-x", "a b", "", "x" * 65):
            proc = self.run_onboard(["app", "--bot", bad])
            self.assertEqual(proc.returncode, 2, bad)
        proc = self.run_onboard(["app", "--bot", "t1", "--brand", "wechat"])
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(self.calls())

    def test_brand_alias_maps_to_lark(self) -> None:
        proc = self.run_onboard(["app", "--bot", "t1", "--brand", "international"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(kv(proc.stdout)["verification_url"].startswith("https://open.larksuite.com/page/launcher?"))
        self.assertEqual(self.calls_matching("config", "init", "--new")[0]["argv"][-2:], ["--brand", "lark"])

    def test_bad_avatar_rejected(self) -> None:
        self.assertEqual(self.run_onboard(["app", "--avatar", "http://x/a.png"]).returncode, 2)
        many = []
        for i in range(7):
            many += ["--avatar", f"https://x/{i}.png"]
        self.assertEqual(self.run_onboard(["app"] + many).returncode, 2)

    def test_no_url_is_an_error(self) -> None:
        self.flag("no-url")
        proc = self.run_onboard(["app", "--bot", "t1"])
        self.assertEqual(proc.returncode, 1)
        out = kv(proc.stdout)
        self.assertEqual(out["status"], "error")
        self.assertIn("boom", out["error"])
        self.assertNotIn("Traceback", proc.stderr)

    def test_lark_cli_missing(self) -> None:
        proc = self.run_onboard(["app", "--bot", "t1"], env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(
            proc.stdout.splitlines(),
            [
                "status: error",
                "error: lark-cli not found",
                "next: npm install -g @larksuite/cli, then run this command again",
            ],
        )

    def test_already_configured_is_idempotent(self) -> None:
        self.run_onboard(["app", "--bot", "t1"])
        self.flag("scan-done")
        self.assertEqual(self.run_onboard(["wait", "--bot", "t1", "--timeout", "20"]).returncode, 0)
        proc = self.run_onboard(["app", "--bot", "t1"])
        self.assertEqual(proc.returncode, 0)
        out = kv(proc.stdout)
        self.assertEqual(out["status"], "ready")
        self.assertEqual(out["app_id"], "cli_fake123")
        self.assertEqual(out["next"], "python3 scripts/onboard.py scopes --bot t1")
        self.assertEqual(len(self.calls_matching("config", "init", "--new")), 1)

    def test_force_replaces_configured_store(self) -> None:
        self.configure("t1")
        proc = self.run_onboard(["app", "--bot", "t1", "--force"])
        self.assertEqual(kv(proc.stdout)["status"], "waiting_for_scan")
        self.assertEqual(len(self.calls_matching("config", "init", "--new")), 1)
        self.assertFalse((self.store("t1") / "app.json").exists())


class LandingPageTests(OnboardCase):
    """The link must open /page/launcher: /page/cli ignores name, avatar and addons."""

    def test_default_is_the_launcher_page(self) -> None:
        proc = self.run_onboard(["app", "--bot", "t1", "--name", "Kala"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        url = kv(proc.stdout)["verification_url"]
        self.assertTrue(url.startswith("https://open.feishu.cn/page/launcher?" + FAKE_URL_QUERY + "&"), url)
        self.assertNotIn("/page/cli", url)
        self.assertIn("&tp=lark_cli_template&", url)
        self.assertIn("&name=Kala&", url)
        qr_call = self.calls_matching("auth", "qrcode")[-1]["argv"][2]
        self.assertTrue(qr_call.startswith(LAUNCHER_BASE + "&"), qr_call)

    def test_page_cli_keeps_the_link_lark_cli_printed(self) -> None:
        proc = self.run_onboard(["app", "--bot", "t1", "--name", "Kala", "--page", "cli"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        url = kv(proc.stdout)["verification_url"]
        self.assertTrue(url.startswith(FAKE_BASE + "&"), url)
        self.assertNotIn("tp=", url)

    def test_landing_url_keeps_the_query_and_other_pages(self) -> None:
        sys.path.insert(0, str(support.SCRIPTS_DIR))
        import onboard  # noqa: E402

        lark = "https://open.larksuite.com/page/cli?user_code=AB-CD&from=cli&tp=custom"
        self.assertEqual(
            onboard.landing_url(lark, "launcher"),
            "https://open.larksuite.com/page/launcher?user_code=AB-CD&from=cli&tp=custom",
        )
        already = "https://open.feishu.cn/page/launcher?user_code=AB-CD"
        self.assertEqual(onboard.landing_url(already, "launcher"), already)
        other = "https://open.feishu.cn/page/something-new?user_code=AB-CD"
        self.assertEqual(onboard.landing_url(other, "launcher"), other)


class WaitTests(OnboardCase):
    def test_timeout(self) -> None:
        start = kv(self.run_onboard(["app", "--bot", "t1"]).stdout)
        proc = self.run_onboard(["wait", "--bot", "t1", "--timeout", "1"])
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        out = kv(proc.stdout)
        self.assertEqual(out["status"], "waiting_for_scan")
        self.assertEqual(out["verification_url"], start["verification_url"])
        self.assertEqual(out["qr_png"], start["qr_png"])
        self.assertIn("Run the same wait command again", out["next"])

    def test_success_finalizes(self) -> None:
        self.run_onboard(["app", "--bot", "t1"])
        self.flag("scan-done")
        proc = self.run_onboard(["wait", "--bot", "t1", "--timeout", "20"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = kv(proc.stdout)
        self.assertEqual(out["status"], "ready")
        self.assertEqual(out["app_id"], "cli_fake123")
        self.assertEqual(out["brand"], "feishu")
        self.assertEqual(out["bot_open_id"], "ou_fakebot")
        self.assertEqual(out["bot_name"], "Fake Bot")
        self.assertEqual(out["store_env"], str(self.store("t1") / "env.sh"))
        self.assertEqual(out["next"], "python3 scripts/onboard.py scopes --bot t1")
        strict = self.calls_matching("config", "strict-mode", "bot", "--global")
        self.assertEqual(len(strict), 1)
        app_json = self.store("t1") / "app.json"
        data = json.loads(app_json.read_text())
        self.assertEqual(
            data,
            {"app_id": "cli_fake123", "brand": "feishu", "bot_open_id": "ou_fakebot", "bot_name": "Fake Bot"},
        )
        self.assertNotIn("secret", app_json.read_text().lower())
        self.assertEqual(self.mode(app_json), 0o600)
        self.assertFalse((self.store("t1") / "app-init.json").exists())

    def test_success_when_scan_finishes_during_wait(self) -> None:
        self.run_onboard(["app", "--bot", "t1"])
        proc = subprocess.Popen(
            [sys.executable, str(support.ONBOARD), "wait", "--bot", "t1", "--timeout", "20"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
            env=dict(self.env),
        )
        import time
        time.sleep(0.5)
        self.flag("scan-done")
        stdout, _ = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(kv(stdout)["status"], "ready")

    def test_wait_is_idempotent_after_success(self) -> None:
        self.run_onboard(["app", "--bot", "t1"])
        self.flag("scan-done")
        self.run_onboard(["wait", "--bot", "t1", "--timeout", "20"])
        proc = self.run_onboard(["wait", "--bot", "t1", "--timeout", "1"])
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(kv(proc.stdout)["status"], "ready")

    def test_bot_info_failure_is_best_effort(self) -> None:
        self.flag("bot-info-fail")
        self.run_onboard(["app", "--bot", "t1"])
        self.flag("scan-done")
        proc = self.run_onboard(["wait", "--bot", "t1", "--timeout", "20"])
        self.assertEqual(proc.returncode, 0)
        out = kv(proc.stdout)
        self.assertEqual(out["bot_open_id"], "unknown")
        self.assertEqual(out["bot_name"], "unknown")

    def test_failure(self) -> None:
        self.run_onboard(["app", "--bot", "t1"])
        self.flag("scan-fail")
        proc = self.run_onboard(["wait", "--bot", "t1", "--timeout", "20"])
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        out = kv(proc.stdout)
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["reason"], "expired")
        self.assertIn("python3 scripts/onboard.py app --bot t1", out["next"])
        self.assertFalse((self.store("t1") / "app.json").exists())

    def test_nothing_to_wait_for(self) -> None:
        proc = self.run_onboard(["wait", "--bot", "t1", "--timeout", "1"])
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(kv(proc.stdout)["status"], "error")


class ImportTests(OnboardCase):
    SECRET = "s3cr3t-VALUE-xyz-0123456789"

    def test_secret_only_via_stdin(self) -> None:
        proc = self.run_onboard(
            ["app", "--bot", "imp", "--app-id", "cli_imp", "--brand", "lark"], input=self.SECRET + "\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = kv(proc.stdout)
        self.assertEqual(out["status"], "ready")
        self.assertEqual(out["app_id"], "cli_imp")
        self.assertEqual(out["brand"], "lark")
        init = self.calls_matching("config", "init", "--app-id")[0]
        self.assertEqual(init["argv"], ["config", "init", "--app-id", "cli_imp", "--app-secret-stdin", "--brand", "lark"])
        self.assertEqual(init["stdin_sha256"], hashlib.sha256(self.SECRET.encode()).hexdigest())
        self.assertEqual(len(self.calls_matching("config", "strict-mode", "bot", "--global")), 1)
        # the secret appears nowhere: output, argv log, state files
        self.assertNotIn(self.SECRET, proc.stdout + proc.stderr)
        for root in (self.home, self.ctl):
            for path in root.rglob("*"):
                if path.is_file():
                    self.assertNotIn(self.SECRET.encode(), path.read_bytes(), str(path))

    def test_empty_stdin_refused(self) -> None:
        proc = self.run_onboard(["app", "--bot", "imp", "--app-id", "cli_imp"], input="\n")
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(self.calls_matching("config", "init"))

    def test_other_app_needs_force(self) -> None:
        self.configure("imp", "cli_one")
        proc = self.run_onboard(["app", "--bot", "imp", "--app-id", "cli_two"], input=self.SECRET)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("cli_one", proc.stdout)
        self.assertNotIn(self.SECRET, proc.stdout)
        proc = self.run_onboard(["app", "--bot", "imp", "--app-id", "cli_two", "--force"], input=self.SECRET)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(kv(proc.stdout)["app_id"], "cli_two")

    def test_legacy_wrapper(self) -> None:
        wrapper = support.SCRIPTS_DIR / "init-bot-store.sh"
        usage = subprocess.run(["bash", str(wrapper)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True, env=self.env)
        self.assertEqual(usage.returncode, 2)
        self.assertIn("usage:", usage.stderr)
        proc = subprocess.run(
            ["bash", str(wrapper), "legacy", "cli_legacy", "international"],
            input=self.SECRET, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, env=self.env,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(kv(proc.stdout)["app_id"], "cli_legacy")
        self.assertEqual(kv(proc.stdout)["brand"], "lark")
        self.assertTrue((self.store("legacy") / "env.sh").is_file())


class ScopesTests(OnboardCase):
    def setUp(self) -> None:
        super().setUp()
        self.configure("t1", "cli_imp")
        self.flag("bridge.json")
        self.set_bridge({"status": "ready", "preconditions": []})

    def set_bridge(self, decision: dict) -> None:
        (self.ctl / "bridge.json").write_text(json.dumps(decision))

    def set_granted(self, entries: list) -> None:
        (self.ctl / "scopes.json").write_text(json.dumps(entries))

    @staticmethod
    def entry(scope: str, types=("tenant",)) -> dict:
        item = {"scope": scope}
        if types is not None:
            item["token_types"] = list(types)
        return item

    def scopes(self, *extra: str):
        return self.run_onboard(["scopes", "--bot", "t1"] + list(extra))

    def test_complete(self) -> None:
        entries = [self.entry(s) for s in REQUIRED[:-1]] + [self.entry(REQUIRED[-1], None)]
        entries.append(self.entry("extra:scope", ["user"]))
        self.set_granted(entries)
        proc = self.scopes()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = kv(proc.stdout)
        self.assertEqual(out["status"], "complete")
        self.assertEqual(out["bridge_ready"], "yes")
        self.assertEqual(out["granted"], f"{len(REQUIRED)}/{len(REQUIRED)}")
        for key in ("apply_url", "apply_qr_png", "permissions_page", "fallback_url", "missing"):
            self.assertNotIn(key, out)
        self.assertEqual(out["next"], "python3 scripts/onboard.py relay --bot t1 --start")
        call = self.calls_matching("api", "GET", "/open-apis/application/v6/applications/cli_imp")[0]
        self.assertIn('{"lang":"zh_cn"}', call["argv"])
        self.assertIn("--as", call["argv"])

    def test_partial(self) -> None:
        missing = [REQUIRED[0], REQUIRED[5], REQUIRED[-1]]
        entries = [self.entry(s) for s in REQUIRED if s not in missing]
        entries.append(self.entry(REQUIRED[7], ["user"]))  # user-only does not count
        missing.append(REQUIRED[7])
        entries = [e for e in entries if not (e["scope"] == REQUIRED[7] and e["token_types"] == ["tenant"])]
        self.set_granted(entries)
        proc = self.scopes()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = kv(proc.stdout)
        expected = [s for s in REQUIRED if s in missing]
        self.assertEqual(out["status"], "partial")
        self.assertEqual(out["bridge_ready"], "yes")
        self.assertEqual(out["granted"], f"{len(REQUIRED) - len(expected)}/{len(REQUIRED)}")
        self.assertEqual(out["missing"].split(","), expected)
        self.assert_qr_only(proc.stdout, expected)
        self.assertTrue(Path(out["apply_qr_png"]).is_file())
        self.assertEqual(Path(out["apply_qr_png"]).name, "scopes-qr.png")
        self.assertEqual(
            out["next"],
            "Continue with: python3 scripts/onboard.py relay --bot t1 --start. In your final setup "
            "message, attach apply_qr_png once and say that scanning it adds the remaining "
            "permissions; do not wait for it.",
        )

    def assert_qr_only(self, stdout: str, expected: list) -> None:
        """With a working QR the output has no long link, and the QR carries the launcher link."""
        out = kv(stdout)
        self.assertEqual(out["permissions_page"], "https://open.feishu.cn/app/cli_imp/auth")
        for key in ("apply_url", "fallback_url", "fallback_note"):
            self.assertNotIn(key, out)
        qr_calls = self.calls_matching("auth", "qrcode")
        self.assertEqual(len(qr_calls), 1)
        url = urlparse(qr_calls[0]["argv"][2])
        self.assertEqual(url.path, "/page/launcher")
        query = parse_qs(url.query)
        self.assertEqual(query["clientID"], ["cli_imp"])
        addons = scope_link.decode_addons(query["addons"][0])
        self.assertEqual(addons["scopes"]["tenant"], expected)
        self.assertEqual(qr_calls[0]["argv"][3:5], ["--output", "scopes-qr.png"])

    def test_partial_with_a_failing_qr_prints_the_links(self) -> None:
        missing = [REQUIRED[0], REQUIRED[5]]
        self.set_granted([self.entry(s) for s in REQUIRED if s not in missing])
        self.flag("qr-fail")
        proc = self.scopes()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = kv(proc.stdout)
        self.assertTrue(out["apply_qr_png"].startswith("unavailable ("), out["apply_qr_png"])
        self.assertNotIn("permissions_page", out)
        url = urlparse(out["apply_url"])
        self.assertEqual(url.path, "/page/launcher")
        query = parse_qs(url.query)
        self.assertEqual(query["clientID"], ["cli_imp"])
        addons = scope_link.decode_addons(query["addons"][0])
        self.assertEqual(addons["scopes"]["tenant"], missing)
        fb = urlparse(out["fallback_url"])
        self.assertEqual(fb.path, "/page/scope-apply")
        self.assertEqual(parse_qs(fb.query)["scopes"][0].split(","), missing)
        self.assertIn("publish a new app version", out["fallback_note"])
        self.assertEqual(
            out["next"],
            "Continue with: python3 scripts/onboard.py relay --bot t1 --start. In your final setup "
            "message, send apply_url exactly as printed; do not wait for it.",
        )

    def test_blocked(self) -> None:
        missing = [REQUIRED[2], REQUIRED[9]]
        self.set_granted([self.entry(s) for s in REQUIRED if s not in missing])
        self.set_bridge(
            {
                "status": "blocked",
                "preconditions": [
                    {"name": "scopes", "status": "blocked", "detail": "missing im:message",
                     "subtype": "missing_scope", "hint": "grant it", "missing_scopes": ["im:message"]},
                    {"name": "auth", "status": "ok", "detail": "fine"},
                ],
            }
        )
        proc = self.scopes()
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        out = kv(proc.stdout)
        self.assertEqual(out["status"], "blocked")
        self.assertEqual(out["bridge_ready"], "no")
        self.assertEqual(lines_for(proc.stdout, "blocker"), ["scopes: missing im:message | grant it"])
        self.assertEqual(
            out["next"],
            "The relay cannot receive messages yet. Send the operator apply_qr_png to scan "
            "(permissions_page is the manual alternative), then run: "
            "python3 scripts/onboard.py scopes --bot t1 --wait 600",
        )
        self.assertTrue(Path(out["apply_qr_png"]).is_file())
        self.assert_qr_only(proc.stdout, missing)

    def test_blocked_with_a_failing_qr_prints_the_link(self) -> None:
        self.set_granted([self.entry(s) for s in REQUIRED])
        self.set_bridge({"status": "blocked", "preconditions": []})
        self.flag("qr-fail")
        proc = self.scopes()
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        out = kv(proc.stdout)
        self.assertIn("apply_url", out)
        self.assertIn("fallback_url", out)
        self.assertTrue(out["apply_qr_png"].startswith("unavailable ("))
        self.assertEqual(
            out["next"],
            "The relay cannot receive messages yet. Send the operator apply_url exactly as "
            "printed, then run: python3 scripts/onboard.py scopes --bot t1 --wait 600",
        )

    def test_unknown(self) -> None:
        (self.ctl / "bridge.json").unlink()
        proc = self.scopes()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = kv(proc.stdout)
        self.assertEqual(out["status"], "unknown")
        self.assertEqual(out["bridge_ready"], "unknown")
        self.assertEqual(out["granted"], "unknown")
        self.assertNotIn("missing", out)
        self.assert_qr_only(proc.stdout, REQUIRED)
        self.assertIn("Continue with", out["next"])

    def test_wait_rechecks_until_deadline(self) -> None:
        self.set_granted([self.entry(s) for s in REQUIRED[:-1]])
        proc = self.scopes("--wait", "1")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(kv(proc.stdout)["status"], "partial")
        self.assertGreaterEqual(len(self.calls_matching("event", "consume")), 2)

    def test_wait_stops_when_complete(self) -> None:
        self.set_granted([self.entry(s) for s in REQUIRED])
        proc = self.scopes("--wait", "30")
        self.assertEqual(kv(proc.stdout)["status"], "complete")
        self.assertEqual(len(self.calls_matching("event", "consume")), 1)

    def test_needs_an_app(self) -> None:
        proc = self.run_onboard(["scopes", "--bot", "nobody"])
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(kv(proc.stdout)["status"], "error")


def make_fake_repo(root: Path, with_start: bool = True) -> Path:
    """A repo layout with the real onboard.py/scope_link.py and a tiny fake relay/."""
    (root / "scripts").mkdir(parents=True)
    for name in ("onboard.py", "scope_link.py"):
        shutil.copy(str(support.SCRIPTS_DIR / name), str(root / "scripts" / name))
    shutil.copytree(str(support.REPO / "scopes"), str(root / "scopes"))
    relay = root / "relay"
    (relay / "tests").mkdir(parents=True)
    (relay / "relay.py").write_text("print('relay')\n")
    (relay / "helper.py").write_text("X = 1\n")
    (relay / "status.sh").write_text("#!/bin/sh\necho status\n")
    (relay / "load-env.sh").write_text("# sourced\n")
    (relay / "notes.txt").write_text("not copied\n")
    (relay / "tests" / "test_x.py").write_text("# not copied\n")
    if with_start:
        (relay / "start.sh").write_text(
            '#!/bin/sh\nD="$(dirname "$0")"\necho started\n[ -f "$D/fail-start" ] && { echo failing; exit 4; }\nexit 0\n'
        )
        (relay / "stop.sh").write_text(
            '#!/bin/sh\nD="$(dirname "$0")"\necho stopped\n[ -f "$D/fail-stop" ] && exit 1\nexit 0\n'
        )
    return root / "scripts" / "onboard.py"


class RelayTests(OnboardCase):
    def setUp(self) -> None:
        super().setUp()
        self.script = make_fake_repo(self.tmp / "repo")
        self.relay_dir = self.tmp / "relay-home"
        self.configure("t1", "cli_imp")

    def relay(self, *extra: str, script=None, bot: str = "t1"):
        return self.run_onboard(
            ["relay", "--bot", bot, "--relay-dir", str(self.relay_dir)] + list(extra),
            script=script or self.script,
        )

    def sourced(self) -> dict:
        proc = subprocess.run(
            ["sh", "-c", f'set -a; . "{self.relay_dir}/relay.env"; env'],
            stdout=subprocess.PIPE, universal_newlines=True, env={"PATH": "/usr/bin:/bin"},
        )
        return dict(l.split("=", 1) for l in proc.stdout.splitlines() if "=" in l)

    def test_install(self) -> None:
        proc = self.relay()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = kv(proc.stdout)
        self.assertEqual(out["status"], "installed")
        self.assertEqual(out["relay_dir"], str(self.relay_dir))
        self.assertEqual(out["relay_env"], str(self.relay_dir / "relay.env"))
        self.assertEqual(out["bot_env"], str(self.store("t1") / "env.sh"))
        self.assertEqual(out["bot_open_id"], "ou_fakebot")
        self.assertEqual(out["webhook"], "not configured")
        self.assertIn(f"webhook --bot t1 --relay-dir {self.relay_dir} --url", out["next"])
        self.assertIn("<POST to URL>", out["next"])
        self.assertEqual(self.mode(self.relay_dir), 0o700)
        for name, mode in [("relay.py", 0o600), ("helper.py", 0o600), ("start.sh", 0o700),
                           ("stop.sh", 0o700), ("status.sh", 0o700), ("load-env.sh", 0o700)]:
            self.assertEqual(self.mode(self.relay_dir / name), mode, name)
        self.assertFalse((self.relay_dir / "tests").exists())
        self.assertFalse((self.relay_dir / "notes.txt").exists())

    def test_relay_env_content(self) -> None:
        self.relay()
        env_file = self.relay_dir / "relay.env"
        self.assertEqual(self.mode(env_file), 0o600)
        text = env_file.read_text()
        self.assertTrue(text.startswith("#"))
        self.assertIn("LARK_BOT_NAME='Fake Bot'", text)  # quoted
        self.assertIn("LARK_BOT_APP_ID=cli_imp", text)
        self.assertNotIn("import-secret-value", text)
        got = self.sourced()
        self.assertEqual(got["LARK_BOT_ENV"], str(self.store("t1") / "env.sh"))
        self.assertEqual(got["LARK_BRAND"], "feishu")
        self.assertEqual(got["LARK_BOT_APP_ID"], "cli_imp")
        self.assertEqual(got["LARK_BOT_OPEN_ID"], "ou_fakebot")
        self.assertEqual(got["LARK_BOT_NAME"], "Fake Bot")

    def test_webhook_env_created_once(self) -> None:
        self.relay()
        webhook_env = self.relay_dir / "webhook.env"
        self.assertEqual(self.mode(webhook_env), 0o600)
        self.assertEqual(
            [l for l in webhook_env.read_text().splitlines() if not l.startswith("#")],
            ["LARK_BOT_WEBHOOK_URL=", "LARK_BOT_WEBHOOK_KEY="],
        )
        webhook_env.write_text("LARK_BOT_WEBHOOK_URL=https://hooks.example/abc\nLARK_BOT_WEBHOOK_KEY=topsecretkey\n")
        proc = self.relay()
        out = kv(proc.stdout)
        self.assertEqual(out["status"], "upgraded")
        self.assertEqual(out["webhook"], "configured")
        self.assertEqual(out["next"], str(self.relay_dir / "status.sh"))
        self.assertEqual(
            webhook_env.read_text(),
            "LARK_BOT_WEBHOOK_URL=https://hooks.example/abc\nLARK_BOT_WEBHOOK_KEY=topsecretkey\n",
        )
        self.assertNotIn("topsecretkey", proc.stdout + proc.stderr)
        self.assertNotIn("hooks.example", proc.stdout + proc.stderr)

    def test_relay_env_idempotent_and_preserves_other_lines(self) -> None:
        self.relay()
        env_file = self.relay_dir / "relay.env"
        first = env_file.read_text()
        self.relay()
        self.assertEqual(env_file.read_text(), first)
        edited = first.replace("LARK_BOT_NAME='Fake Bot'", "LARK_BOT_NAME=old") \
            + "LARK_BOT_WEBHOOK_URL=https://x.example/y\nEXTRA=1\n"
        env_file.write_text(edited)
        self.relay()
        text = env_file.read_text()
        self.assertIn("LARK_BOT_WEBHOOK_URL=https://x.example/y\n", text)
        self.assertIn("\nEXTRA=1\n", text)
        self.assertEqual(text.count("LARK_BOT_NAME="), 1)
        self.assertIn("LARK_BOT_NAME='Fake Bot'", text)
        self.assertEqual(text.count("LARK_BOT_APP_ID="), 1)
        keys = [l.split("=")[0] for l in text.splitlines() if "=" in l and not l.startswith("#")]
        self.assertEqual(keys[:5], [l.split("=")[0] for l in first.splitlines() if "=" in l and not l.startswith("#")])

    def test_default_relay_dir_follows_the_bot_name(self) -> None:
        proc = self.run_onboard(["relay", "--bot", "t1"], script=self.script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(kv(proc.stdout)["relay_dir"], str(self.home / ".lark-relay-t1"))
        self.assertTrue((self.home / ".lark-relay-t1" / "relay.py").is_file())
        self.configure("lark-bot", "cli_main")
        proc = self.run_onboard(["relay"], script=self.script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(kv(proc.stdout)["relay_dir"], str(self.home / ".lark-relay"))
        proc = self.run_onboard(
            ["webhook", "--bot", "t1", "--url", "https://hooks.example.com/t1"],
            env={"LARK_BOT_WEBHOOK_KEY": "k-t1"}, script=self.script,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(kv(proc.stdout)["file"], str(self.home / ".lark-relay-t1" / "webhook.env"))

    def test_relay_dir_of_another_bot_is_refused(self) -> None:
        self.relay()
        before = (self.relay_dir / "relay.env").read_text()
        self.configure("t2", "cli_other")
        result = self.relay(bot="t2")
        self.assertEqual(result.returncode, 1)
        self.assertIn("status: error", result.stdout)
        self.assertIn("already belongs to another bot", result.stdout)
        self.assertIn("--relay-dir ~/.lark-relay-t2", result.stdout)
        self.assertEqual((self.relay_dir / "relay.env").read_text(), before)

    def test_webhook_fallback_from_relay_env(self) -> None:
        self.relay()
        env_file = self.relay_dir / "relay.env"
        env_file.write_text(env_file.read_text() + "LARK_BOT_WEBHOOK_URL=https://x.example/y\n")
        self.assertEqual(kv(self.relay().stdout)["webhook"], "not configured")
        proc = self.relay_with_env({"LARK_BOT_WEBHOOK_KEY": "Bearer envkey"})
        self.assertEqual(kv(proc.stdout)["webhook"], "configured")
        self.assertNotIn("envkey", proc.stdout)

    def relay_with_env(self, env: dict):
        return self.run_onboard(
            ["relay", "--bot", "t1", "--relay-dir", str(self.relay_dir)], script=self.script, env=env
        )

    def test_unknown_open_id_is_omitted_then_filled(self) -> None:
        self.configure("t2", "cli_two")  # configured while bot info works
        shutil.rmtree(str(self.store("t2") / "app.json"), ignore_errors=True)
        (self.store("t2") / "app.json").unlink()
        self.flag("bot-info-fail")
        proc = self.relay(bot="t2")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(kv(proc.stdout)["bot_open_id"], "unknown")
        self.assertNotIn("LARK_BOT_OPEN_ID", (self.relay_dir / "relay.env").read_text())
        self.unflag("bot-info-fail")
        proc = self.relay(bot="t2")
        self.assertEqual(kv(proc.stdout)["bot_open_id"], "ou_fakebot")
        self.assertIn("LARK_BOT_OPEN_ID=ou_fakebot", (self.relay_dir / "relay.env").read_text())
        data = json.loads((self.store("t2") / "app.json").read_text())
        self.assertEqual(data["bot_open_id"], "ou_fakebot")

    def test_lazy_app_json(self) -> None:
        (self.store("t1") / "app.json").unlink()
        proc = self.relay()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue((self.store("t1") / "app.json").is_file())
        self.assertEqual(kv(proc.stdout)["status"], "installed")

    def test_start_runs_stop_then_start(self) -> None:
        proc = self.relay("--start")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        lines = proc.stdout.splitlines()
        self.assertLess(lines.index("relay: stopped"), lines.index("relay: started"))
        self.assertTrue(lines[-1].startswith("next: "))

    def test_start_failure_sets_the_exit_code(self) -> None:
        self.relay()
        (self.relay_dir / "fail-start").write_text("")
        proc = self.relay("--start")
        self.assertEqual(proc.returncode, 4)
        self.assertIn("relay: failing", proc.stdout)
        self.assertIn("relay: stopped", proc.stdout)

    def test_a_relay_that_does_not_stop_is_an_error(self) -> None:
        self.relay()
        (self.relay_dir / "fail-stop").write_text("")
        proc = self.relay("--start")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("error: the running relay did not stop", proc.stdout)
        self.assertNotIn("relay: started", proc.stdout)

    def test_start_scripts_missing(self) -> None:
        script = make_fake_repo(self.tmp / "repo2", with_start=False)
        proc = self.relay("--start", script=script)
        self.assertEqual(proc.returncode, 1)
        self.assertTrue(any(l.startswith("error: ") and "start.sh" in l for l in proc.stdout.splitlines()))


class WebhookTests(OnboardCase):
    URL = "https://hooks.example.com/trigger/abc"
    KEY = "k-9f8e7d6c"

    def setUp(self) -> None:
        super().setUp()
        self.relay_dir = self.tmp / "relay-home"
        self.file = self.relay_dir / "webhook.env"

    def webhook(self, *args: str, input=None, env=None):
        return self.run_onboard(["webhook", "--relay-dir", str(self.relay_dir)] + list(args), input=input, env=env)

    def assert_no_leak(self, proc, *values: str) -> None:
        for value in values:
            self.assertNotIn(value, proc.stdout)
            self.assertNotIn(value, proc.stderr)

    def test_key_from_env_with_prefix_stripped(self) -> None:
        for raw in ("Authorization: Bearer " + self.KEY, "Bearer " + self.KEY, "bearer " + self.KEY,
                    "authorization:bearer  " + self.KEY + "\n", "  " + self.KEY + " "):
            proc = self.webhook("--url", self.URL, env={"LARK_BOT_WEBHOOK_KEY": raw})
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            out = kv(proc.stdout)
            self.assertEqual(out["status"], "saved")
            self.assertEqual((out["url"], out["key"]), ("set", "set"))
            self.assertEqual(out["file"], str(self.file))
            self.assertIn("status.sh", out["next"])
            self.assertEqual(support_values(self.file), (self.URL, self.KEY))
            self.assert_no_leak(proc, self.KEY, self.URL)
        self.assertEqual(self.mode(self.file), 0o600)
        self.assertEqual(self.mode(self.relay_dir), 0o700)
        self.assertEqual([p.name for p in self.relay_dir.iterdir()], ["webhook.env"])

    def test_key_env_names_another_variable(self) -> None:
        env = {"LARK_BOT_WEBHOOK_KEY": "key-of-another-bot", "LARK_BOT_WEBHOOK_KEY_SALES": self.KEY}
        proc = self.webhook("--url", self.URL, "--key-env", "LARK_BOT_WEBHOOK_KEY_SALES", env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(support_values(self.file), (self.URL, self.KEY))
        self.assert_no_leak(proc, self.KEY, "key-of-another-bot")
        missing = self.webhook("--url", self.URL, "--key-env", "NOT_SET_ANYWHERE")
        self.assertEqual(missing.returncode, 0)  # the key already in the file is kept
        bad = self.webhook("--url", self.URL, "--key-env", "not a name")
        self.assertNotEqual(bad.returncode, 0)

    def test_key_from_stdin(self) -> None:
        proc = self.webhook("--url", self.URL, "--key-stdin", input="Authorization: Bearer " + self.KEY + "\n")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(support_values(self.file), (self.URL, self.KEY))
        self.assert_no_leak(proc, self.KEY)
        self.assertEqual(self.mode(self.file), 0o600)

    def test_url_from_env(self) -> None:
        proc = self.webhook(env={"LARK_BOT_WEBHOOK_URL": self.URL, "LARK_BOT_WEBHOOK_KEY": self.KEY})
        self.assertEqual(kv(proc.stdout)["status"], "saved")

    def test_incomplete(self) -> None:
        proc = self.webhook("--url", self.URL)
        self.assertEqual(proc.returncode, 3)
        out = kv(proc.stdout)
        self.assertEqual(out["status"], "incomplete")
        self.assertEqual((out["url"], out["key"]), ("set", "missing"))
        self.assertIn("LARK_BOT_WEBHOOK_KEY", out["next"])
        self.assertIn("--key-stdin", out["next"])
        self.file.unlink()  # the saved URL would otherwise be reused
        proc = self.webhook(env={"LARK_BOT_WEBHOOK_KEY": self.KEY})
        self.assertEqual(proc.returncode, 3)
        out = kv(proc.stdout)
        self.assertEqual((out["url"], out["key"]), ("missing", "set"))
        self.assertIn("--url", out["next"])
        self.assert_no_leak(proc, self.KEY)

    def test_existing_values_are_kept(self) -> None:
        self.webhook("--url", self.URL, env={"LARK_BOT_WEBHOOK_KEY": self.KEY})
        proc = self.webhook("--url", "https://new.example.com/x")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(support_values(self.file), ("https://new.example.com/x", self.KEY))
        proc = self.webhook(input="", env={"LARK_BOT_WEBHOOK_KEY": "rotated"})
        self.assertEqual(support_values(self.file), ("https://new.example.com/x", "rotated"))

    def test_non_https_rejected(self) -> None:
        for url in ("http://hooks.example.com/x", "ftp://x", "hooks.example.com/x"):
            proc = self.webhook("--url", url, env={"LARK_BOT_WEBHOOK_KEY": self.KEY})
            self.assertEqual(proc.returncode, 2, url)
            self.assertEqual(kv(proc.stdout)["status"], "error")
            self.assertFalse(self.file.exists())
            self.assert_no_leak(proc, self.KEY)

    def test_key_is_never_accepted_on_argv(self) -> None:
        proc = self.webhook("--url", self.URL, "--key", self.KEY)
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(self.file.exists())


def support_values(path: Path):
    values = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            values[k] = v.strip("'")
    return values["LARK_BOT_WEBHOOK_URL"], values["LARK_BOT_WEBHOOK_KEY"]



class RoutineTests(OnboardCase):
    def routine(self, kind: str, *extra: str, script=None, env=None):
        return self.run_onboard(["routine", kind] + list(extra), script=script, env=env)

    @staticmethod
    def template_parts(name: str):
        lines = (support.REPO / "templates" / name).read_text().splitlines()
        cut = lines.index("---")
        return lines[:cut], [l for l in lines[cut + 1:]]

    def test_handler_prints_only_the_instruction(self) -> None:
        proc = self.routine("handler", env={"PATH": "/usr/bin:/bin"})  # no lark-cli needed
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        above, below = self.template_parts("handler-routine-prompt.md")
        first = next(l for l in below if l.strip())
        self.assertTrue(proc.stdout.startswith(first + "\n"), proc.stdout[:80])
        for line in above:
            if line.strip():
                self.assertNotIn(line, proc.stdout.splitlines())
        self.assertNotIn("\n---\n", proc.stdout)
        self.assertIn("`relay_test`", proc.stdout)
        self.assertTrue(proc.stdout.endswith("\n") and not proc.stdout.endswith("\n\n"))
        self.assertNotIn("next: ", proc.stdout)
        self.assertNotIn("status: ", proc.stdout)

    def test_health_check_follows_the_relay_directory(self) -> None:
        default = self.routine("health-check").stdout
        self.assertIn("~/.lark-relay/status.sh", default)
        sales = self.routine("health-check", "--bot", "sales").stdout
        self.assertIn("~/.lark-relay-sales/status.sh", sales)
        self.assertNotIn("~/.lark-relay/", sales)
        self.assertNotIn("~/.lark-relay ", sales)
        custom = self.routine("health-check", "--relay-dir", "/opt/relay").stdout
        self.assertIn("/opt/relay/status.sh", custom)
        self.assertNotIn(".lark-relay", custom)
        inside = self.routine("health-check", "--relay-dir", str(self.home / "my relay")).stdout
        self.assertIn("~/my relay/status.sh", inside)

    def test_longer_names_are_not_replaced(self) -> None:
        repo = self.tmp / "repo"
        script = make_fake_repo(repo)
        (repo / "templates").mkdir()
        (repo / "templates" / "health-check-routine-prompt.md").write_text(
            "head\n---\nrun ~/.lark-relay/a, ~/.lark-relay-other/b, ~/.lark-relay_x and ~/.lark-relay.\n"
        )
        text = self.routine("health-check", "--relay-dir", "/opt/relay", script=script).stdout
        self.assertEqual(
            text, "run /opt/relay/a, ~/.lark-relay-other/b, ~/.lark-relay_x and /opt/relay.\n"
        )

    def test_template_without_a_separator_prints_everything(self) -> None:
        repo = self.tmp / "repo"
        script = make_fake_repo(repo)
        (repo / "templates").mkdir()
        (repo / "templates" / "handler-routine-prompt.md").write_text("only text\n")
        self.assertEqual(self.routine("handler", script=script).stdout, "only text\n")

    def test_missing_template_is_an_error(self) -> None:
        script = make_fake_repo(self.tmp / "repo")
        proc = self.routine("handler", script=script)
        self.assertEqual(proc.returncode, 1)
        out = kv(proc.stdout)
        self.assertEqual(out["status"], "error")
        self.assertIn("error", out)

    def test_unknown_kind_is_refused(self) -> None:
        self.assertNotEqual(self.routine("other").returncode, 0)


class WebhookServer:
    """A loopback HTTP server that answers every POST with one status and records the requests."""

    def __init__(self, status: int) -> None:
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                outer.requests.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "content_type": self.headers.get("Content-Type"),
                        "body": json.loads(self.rfile.read(length) or b"{}"),
                    }
                )
                self.send_response(status)
                self.end_headers()
                self.wfile.write(b"x")

            def log_message(self, *args) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


class WebhookCheckTests(OnboardCase):
    SECRET_PATH = "/hook/s3cr3t-path-token"
    KEY = "k-check-1a2b3c"

    def setUp(self) -> None:
        super().setUp()
        self.relay_dir = self.tmp / "relay-home"
        self.file = self.relay_dir / "webhook.env"

    def serve(self, status: int) -> WebhookServer:
        server = WebhookServer(status)
        self.addCleanup(server.close)
        return server

    def run_check(self, port: int, *extra: str):
        proc = self.run_onboard(
            ["webhook", "--relay-dir", str(self.relay_dir), "--url", f"http://127.0.0.1:{port}{self.SECRET_PATH}"]
            + list(extra),
            env={"LARK_BOT_WEBHOOK_KEY": self.KEY, "ONBOARD_SKIP_WEBHOOK_TEST": "0"},
        )
        for value in (self.KEY, self.SECRET_PATH):
            self.assertNotIn(value, proc.stdout)
            self.assertNotIn(value, proc.stderr)
        self.assertEqual(support_values(self.file)[1], self.KEY)  # written in every case
        return proc

    def test_accepted(self) -> None:
        server = self.serve(200)
        proc = self.run_check(server.port)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = kv(proc.stdout)
        self.assertEqual((out["status"], out["test"]), ("saved", "accepted (HTTP 200)"))
        self.assertIn("status.sh", out["next"])
        self.assertEqual(len(server.requests), 1)
        request = server.requests[0]
        self.assertEqual(request["path"], self.SECRET_PATH)
        self.assertEqual(request["authorization"], "Bearer " + self.KEY)
        self.assertEqual(request["content_type"], "application/json")
        self.assertEqual(request["body"]["type"], "relay_test")
        self.assertIn("Do nothing", request["body"]["instructions"])

    def test_unauthorized(self) -> None:
        for status in (401, 403):
            server = self.serve(status)
            proc = self.run_check(server.port)
            self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
            out = kv(proc.stdout)
            self.assertEqual((out["status"], out["test"]), ("rejected", f"rejected (HTTP {status})"))
            self.assertIn("does not match", out["next"])

    def test_other_http_status(self) -> None:
        server = self.serve(500)
        proc = self.run_check(server.port)
        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
        out = kv(proc.stdout)
        self.assertEqual((out["status"], out["test"]), ("rejected", "failed (HTTP 500)"))
        self.assertIn("not paused", out["next"])

    def test_unreachable(self) -> None:
        server = WebhookServer(200)
        port = server.port
        server.close()  # nothing listens on this port any more
        proc = self.run_check(port)
        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
        out = kv(proc.stdout)
        self.assertEqual(out["status"], "rejected")
        self.assertRegex(out["test"], r"^unreachable \(\w+\)$")
        self.assertIn("not paused", out["next"])

    def test_no_test_flag(self) -> None:
        server = self.serve(200)
        proc = self.run_check(server.port, "--no-test")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = kv(proc.stdout)
        self.assertEqual((out["status"], out["test"]), ("saved", "skipped"))
        self.assertEqual(server.requests, [])

    def test_loopback_urls_are_accepted_and_others_are_not(self) -> None:
        env = {"LARK_BOT_WEBHOOK_KEY": self.KEY}
        for url in ("http://127.0.0.1:8080/hook", "http://localhost:8080/hook", "http://localhost/x",
                    "http://127.0.0.1"):
            proc = self.run_onboard(["webhook", "--relay-dir", str(self.relay_dir), "--url", url], env=env)
            self.assertEqual(proc.returncode, 0, url + proc.stdout)
        for url in ("http://example.com/x", "http://127.0.0.1.example.com/x", "http://localhost@example.com/x"):
            proc = self.run_onboard(["webhook", "--relay-dir", str(self.relay_dir), "--url", url], env=env)
            self.assertEqual(proc.returncode, 2, url)


class NextCommandTests(OnboardCase):
    """next: lines leave out --bot for the default bot and keep it for any other."""

    def next_lines(self, bot: str) -> dict:
        flags = [] if bot == "lark-bot" else ["--bot", bot]
        result = {}
        proc = self.run_onboard(["app"] + flags)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result["app"] = kv(proc.stdout)["next"]
        self.flag("scan-done")
        proc = self.run_onboard(["wait", "--timeout", "20"] + flags)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result["wait"] = kv(proc.stdout)["next"]
        result["scopes"] = kv(self.run_onboard(["scopes"] + flags).stdout)["next"]
        result["relay"] = kv(self.run_onboard(["relay"] + flags).stdout)["next"]
        return result

    def test_default_bot_has_no_bot_flag(self) -> None:
        for name, line in self.next_lines("lark-bot").items():
            self.assertNotIn("--bot", line, name)
            self.assertIn("python3 scripts/onboard.py", line, name)

    def test_named_bot_keeps_the_bot_flag(self) -> None:
        lines = self.next_lines("sales")
        self.assertIn("python3 scripts/onboard.py wait --bot sales", lines["app"])
        self.assertIn("python3 scripts/onboard.py scopes --bot sales", lines["wait"])
        self.assertIn("python3 scripts/onboard.py relay --bot sales --start", lines["scopes"])
        self.assertIn("python3 scripts/onboard.py webhook --bot sales --url '<POST to URL>'", lines["relay"])

    def test_app_next_asks_for_one_message_with_the_qr(self) -> None:
        nxt = kv(self.run_onboard(["app"]).stdout)["next"]
        self.assertEqual(
            nxt,
            "Send the operator ONE message now: attach the QR image (scanning it with Feishu or "
            "Lark creates the app with every permission in one go) and paste verification_url "
            "exactly as printed for anyone who cannot scan. Then immediately run: "
            "python3 scripts/onboard.py wait",
        )

    @staticmethod
    def relay_next(bot_flag: str, flags: str) -> str:
        return (
            "Create the two routines; their instructions are printed by: "
            f"python3 scripts/onboard.py routine handler{bot_flag} and "
            f"python3 scripts/onboard.py routine health-check{bot_flag}. "
            "Then, in this order: 1) tell the operator where the webhook URL and key are "
            "(the handler routine's Webhook section), 2) request a secure secret named "
            f"LARK_BOT_WEBHOOK_KEY, 3) run: python3 scripts/onboard.py webhook{flags} "
            "--url '<POST to URL>'. Never put the key on the command line."
        )

    def test_relay_next_when_webhook_is_missing(self) -> None:
        self.configure("lark-bot", "cli_imp")
        nxt = kv(self.run_onboard(["relay"]).stdout)["next"]
        self.assertEqual(nxt, self.relay_next("", ""))

    def test_relay_next_for_a_named_bot_with_a_relay_dir(self) -> None:
        self.configure("sales", "cli_imp")
        relay_dir = self.tmp / "elsewhere"
        nxt = kv(self.run_onboard(["relay", "--bot", "sales", "--relay-dir", str(relay_dir)]).stdout)["next"]
        self.assertEqual(nxt, self.relay_next(" --bot sales", f" --bot sales --relay-dir {relay_dir}"))

    def test_relay_next_with_only_a_relay_dir(self) -> None:
        self.configure("lark-bot", "cli_imp")
        relay_dir = self.tmp / "elsewhere"
        nxt = kv(self.run_onboard(["relay", "--relay-dir", str(relay_dir)]).stdout)["next"]
        self.assertEqual(nxt, self.relay_next("", f" --relay-dir {relay_dir}"))


class WebhookCheckOnlyTests(OnboardCase):
    """webhook --check tests what the relay already uses and writes nothing."""

    KEY = "k-existing-9z8y"
    PATH = "/hook/existing-path-token"

    def setUp(self) -> None:
        super().setUp()
        self.relay_dir = self.tmp / "relay-home"
        self.relay_dir.mkdir()
        self.file = self.relay_dir / "webhook.env"

    def save(self, port: int) -> str:
        text = f"LARK_BOT_WEBHOOK_URL=http://127.0.0.1:{port}{self.PATH}\nLARK_BOT_WEBHOOK_KEY={self.KEY}\n"
        self.file.write_text(text)
        return text

    def check(self, *extra: str, env=None):
        merged = {"ONBOARD_SKIP_WEBHOOK_TEST": "0", "LARK_BOT_WEBHOOK_KEY": "stale-key-from-environment"}
        merged.update(env or {})
        proc = self.run_onboard(["webhook", "--relay-dir", str(self.relay_dir), "--check"] + list(extra), env=merged)
        for value in (self.KEY, self.PATH, "stale-key-from-environment"):
            self.assertNotIn(value, proc.stdout)
            self.assertNotIn(value, proc.stderr)
        return proc

    def test_accepted_uses_the_file_and_leaves_it_alone(self) -> None:
        server = WebhookServer(200)
        self.addCleanup(server.close)
        before = self.save(server.port)
        proc = self.check()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = kv(proc.stdout)
        self.assertEqual((out["status"], out["test"]), ("ok", "accepted (HTTP 200)"))
        self.assertIn("not changed", out["file"])
        self.assertEqual(self.file.read_text(), before)
        # The key in the file wins over a different one in the environment.
        self.assertEqual(server.requests[0]["authorization"], "Bearer " + self.KEY)
        self.assertEqual(server.requests[0]["body"]["type"], "relay_test")

    def test_rejected(self) -> None:
        server = WebhookServer(401)
        self.addCleanup(server.close)
        before = self.save(server.port)
        proc = self.check()
        self.assertEqual(proc.returncode, 4)
        out = kv(proc.stdout)
        self.assertEqual((out["status"], out["test"]), ("rejected", "rejected (HTTP 401)"))
        self.assertIn("webhook --url", out["next"])
        self.assertEqual(self.file.read_text(), before)

    def test_nothing_configured(self) -> None:
        proc = self.check(env={"LARK_BOT_WEBHOOK_KEY": ""})
        self.assertEqual(proc.returncode, 3)
        out = kv(proc.stdout)
        self.assertEqual((out["status"], out["url"], out["key"]), ("incomplete", "missing", "missing"))
        self.assertFalse(self.file.exists())

    def test_check_takes_no_new_values(self) -> None:
        proc = self.check("--url", "https://hooks.example.com/x")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("status: error", proc.stdout)
        self.assertFalse(self.file.exists())


class HelpTests(unittest.TestCase):
    def test_help_for_every_command(self) -> None:
        for cmd in ([], ["app"], ["wait"], ["scopes"], ["relay"], ["webhook"], ["routine"]):
            proc = subprocess.run(
                [sys.executable, str(support.ONBOARD)] + cmd + ["--help"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
            )
            self.assertEqual(proc.returncode, 0, cmd)
            self.assertIn("usage:", proc.stdout)


if __name__ == "__main__":
    unittest.main()
