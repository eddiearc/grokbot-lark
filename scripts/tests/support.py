"""Shared test support: a fake lark-cli and a harness that runs onboard.py against it.

The fake never touches the network. HOME points at a temp dir and the fake sits
first on PATH, so the real lark-cli can never be reached.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Sequence

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent
REPO = SCRIPTS_DIR.parent
ONBOARD = SCRIPTS_DIR / "onboard.py"
sys.path.insert(0, str(SCRIPTS_DIR))

import scope_link  # noqa: E402

FAKE_URL_QUERY = "user_code=ABCD-EFGH&lpv=1.0.100&ocv=1.0.100&from=cli"

FAKE_CLI = r'''#!__PYTHON__
import hashlib, json, os, sys, time

ctl = os.environ["FAKE_CONTROL"]
scopes_path = os.path.join(ctl, "scopes.json")
CONFIG_SCOPE = "application:application:patch"
argv = sys.argv[1:]
cfg = os.environ.get("LARKSUITE_CLI_CONFIG_DIR", "")
stdin_sha = None


def log(extra=None):
    rec = {
        "argv": argv,
        "config_dir": cfg,
        "data_dir": os.environ.get("LARKSUITE_CLI_DATA_DIR"),
        "log_dir": os.environ.get("LARKSUITE_CLI_LOG_DIR"),
        "cwd": os.getcwd(),
        "pid": os.getpid(),
    }
    if extra:
        rec.update(extra)
    with open(os.path.join(ctl, "calls.jsonl"), "a") as f:
        f.write(json.dumps(rec) + "\n")


def flag(name):
    return os.path.exists(os.path.join(ctl, name))


def marker():
    p = os.path.join(cfg, "configured.json")
    if os.path.exists(p):
        return json.load(open(p))
    return None


def write_marker(app_id, brand):
    os.makedirs(cfg, exist_ok=True)
    json.dump({"appId": app_id, "brand": brand}, open(os.path.join(cfg, "configured.json"), "w"))


def read_scopes():
    if not os.path.exists(scopes_path):
        return []
    return json.load(open(scopes_path))


def say(obj, stream=sys.stdout):
    stream.write(json.dumps(obj) + "\n")
    stream.flush()


def fail(error, code):
    # As the real CLI does for a failed API call: nothing on stdout, the envelope on stderr.
    sys.stderr.write(json.dumps({"ok": False, "identity": "bot", "error": error}, indent=2) + "\n")
    sys.exit(code)


def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None


head = argv[:2]
if argv[:2] == ["config", "init"] and "--new" in argv:
    log()
    brand = opt("--brand") or "feishu"
    if flag("no-url"):
        sys.stderr.write("boom: cannot reach the registration service\n")
        sys.exit(1)
    host = "open.larksuite.com" if brand == "lark" else "open.feishu.cn"
    sys.stderr.write("Scan the QR code:\n")
    sys.stderr.write("  ██▀▀██  ▄▄  \n")
    sys.stderr.write("  ██▄▄██  ▀▀  \n")
    sys.stderr.write("  https://%s/page/cli?%s\n" % (host, "__QUERY__"))
    sys.stderr.flush()
    end = time.time() + 40
    while time.time() < end:
        if flag("scan-done"):
            write_marker("cli_fake123", brand)
            say({"appId": "cli_fake123", "appSecret": "****", "brand": brand})
            sys.exit(0)
        if flag("scan-fail"):
            say({"ok": False, "error": {"type": "auth", "message": "device code expired, start again"}}, sys.stderr)
            sys.exit(3)
        time.sleep(0.05)
    sys.exit(9)
elif argv[:2] == ["config", "init"] and "--app-secret-stdin" in argv:
    data = sys.stdin.read()
    log({"stdin_sha256": hashlib.sha256(data.encode()).hexdigest(), "stdin_len": len(data)})
    write_marker(opt("--app-id"), opt("--brand") or "feishu")
elif argv[:2] == ["config", "show"]:
    log()
    m = marker()
    if m:
        say({"appId": m["appId"], "appSecret": "****", "brand": m["brand"]})
    else:
        say({"ok": False, "error": {"type": "config", "subtype": "not_configured", "message": "not configured"}})
        sys.exit(1)
elif argv[:2] == ["config", "strict-mode"]:
    log()
    open(os.path.join(cfg, "strict"), "w").write("bot")
elif argv[:1] == ["whoami"]:
    log()
    m = marker() or {"appId": "", "brand": "feishu"}
    ident = "bot" if os.path.exists(os.path.join(cfg, "strict")) else "user"
    say({"appId": m["appId"], "brand": m["brand"], "identity": ident,
         "identitySource": "strict_mode", "available": True})
elif argv[:2] == ["auth", "qrcode"]:
    log()
    if flag("qr-fail"):
        sys.stderr.write("qr generation failed\n")
        sys.exit(1)
    out = opt("--output")
    assert not os.path.isabs(out), "output must be relative"
    open(out, "wb").write(b"\x89PNG fake")
    say({"file_path": os.path.abspath(out), "ok": True})
elif argv[:2] == ["api", "GET"] and argv[2].endswith("/bot/v3/info"):
    log()
    if flag("bot-info-fail"):
        sys.stderr.write("forbidden\n")
        sys.exit(1)
    bot = {"activate_status": 2, "app_name": "Fake Bot", "open_id": "ou_fakebot"}
    if flag("bot-info-legacy"):
        say({"code": 0, "msg": "ok", "bot": bot})  # the raw API shape, as older lark-cli printed it
    else:
        say({"ok": True, "identity": "bot", "data": bot})  # lark-cli lifts the fields into "data"
elif argv[:2] == ["api", "GET"] and argv[2] == "/open-apis/application/v6/scopes":
    log()
    if not os.path.exists(scopes_path):
        fail({"type": "api", "code": 212100, "message": "app not install"}, 1)
    say({"ok": True, "identity": "bot", "data": {"scopes": read_scopes()}})
elif argv[:2] == ["api", "PATCH"] and argv[2].endswith("/config"):
    body = json.loads(sys.stdin.read())
    log({"body": body})
    items = read_scopes()
    if not any(i["scope_name"] == CONFIG_SCOPE and i["grant_status"] == 1 for i in items):
        fail({"type": "authorization", "subtype": "app_scope_not_applied", "code": 99991672,
              "message": "access denied: app cli_x has not applied for the required scope(s): " + CONFIG_SCOPE,
              "missing_scopes": [CONFIG_SCOPE]}, 3)
    if flag("patch-fail"):
        fail({"type": "api", "subtype": "unknown", "code": 210031, "message": "invalid request"}, 1)
    status = 2 if flag("patch-pending") else 1
    known = {i["scope_name"] for i in items if i["scope_type"] == "tenant"}
    for item in body["scope"]["add_scopes"]:
        if item["scope_name"] not in known:
            items.append({"grant_status": status, "scope_name": item["scope_name"], "scope_type": item["token_type"]})
    json.dump(items, open(scopes_path, "w"))
    say({"ok": True, "identity": "bot", "data": {}})
elif argv[:2] == ["api", "POST"] and argv[2].endswith("/publish"):
    body = json.loads(sys.stdin.read())
    log({"body": body})
    if flag("publish-fail"):
        fail({"type": "api", "subtype": "unknown", "code": 210302,
              "message": "Unable to submit the release request, as the app is currently under review."}, 1)
    if flag("publish-grants"):
        items = read_scopes()
        for i in items:
            if i["grant_status"] == 2:
                i["grant_status"] = 1
        json.dump(items, open(scopes_path, "w"))
    say({"ok": True, "identity": "bot", "data": {"version": "1.0.3", "version_id": "oav_x"}})
elif argv[:2] == ["event", "consume"]:
    log()
    p = os.path.join(ctl, "bridge.json")
    if not os.path.exists(p):
        sys.stderr.write("dry-run unavailable\n")
        sys.exit(1)
    say({"ok": True, "data": {"decision": json.load(open(p))}})
else:
    log()
    sys.stderr.write("fake lark-cli: unsupported %r\n" % (argv,))
    sys.exit(64)
'''


def kv(text: str) -> Dict[str, str]:
    """Parse `key: value` lines (last one wins)."""
    result: Dict[str, str] = {}
    for line in text.splitlines():
        if ": " in line:
            key, _, value = line.partition(": ")
            result[key.strip()] = value.strip()
    return result


def lines_for(text: str, key: str) -> List[str]:
    prefix = key + ": "
    return [line[len(prefix):] for line in text.splitlines() if line.startswith(prefix)]


class OnboardCase(unittest.TestCase):
    """Temp HOME, fake lark-cli first on PATH, cleanup of leftover processes."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="onboard-test-")).resolve()
        self.home = self.tmp / "home"
        self.bin = self.tmp / "bin"
        self.ctl = self.tmp / "ctl"
        for d in (self.home, self.bin, self.ctl):
            d.mkdir()
        cli = self.bin / "lark-cli"
        cli.write_text(FAKE_CLI.replace("__PYTHON__", sys.executable).replace("__QUERY__", FAKE_URL_QUERY))
        cli.chmod(0o755)
        self.qr_dirs: List[str] = []
        self.env = {
            "HOME": str(self.home),
            "PATH": str(self.bin) + os.pathsep + "/usr/bin" + os.pathsep + "/bin",
            "FAKE_CONTROL": str(self.ctl),
            "ONBOARD_POLL_SECONDS": "0.1",
            "ONBOARD_SETTLE_SECONDS": "0.3",
            "ONBOARD_SKIP_NODE_SDK": "1",
            "ONBOARD_URL_TIMEOUT": "10",
            "PYTHONDONTWRITEBYTECODE": "1",
            # webhook tests that exercise the connectivity check set this to 0 and use a loopback server
            "ONBOARD_SKIP_WEBHOOK_TEST": "1",
            # must be ignored by onboard.py for every lark-cli call
            "LARKSUITE_CLI_CONFIG_DIR": "/nonexistent/caller-config",
        }
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        for state in self.home.glob(".lark-bots/*/app-init.json"):
            try:
                pid = json.loads(state.read_text())["pid"]
                os.killpg(pid, signal.SIGKILL)
            except Exception:
                pass
        # also kill any fake registration process the log mentions
        for rec in self.calls():
            if rec["argv"][:3] == ["config", "init", "--new"]:
                try:
                    os.kill(rec["pid"], signal.SIGKILL)
                except Exception:
                    pass
        for d in self.qr_dirs:
            shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- running

    def run_onboard(
        self,
        args: List[str],
        input: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        script: Optional[Path] = None,
        timeout: float = 60,
    ) -> "subprocess.CompletedProcess[str]":
        full_env = dict(self.env)
        if env:
            full_env.update(env)
        proc = subprocess.run(
            [sys.executable, str(script or ONBOARD)] + args,
            input=input if input is not None else "",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            env=full_env,
            timeout=timeout,
        )
        for line in proc.stdout.splitlines():
            if "qr_png: " in line:
                path = line.split("qr_png: ", 1)[1].strip()
                if path.startswith("/"):
                    self.qr_dirs.append(str(Path(path).parent))
        return proc

    def calls(self) -> List[dict]:
        path = self.ctl / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def calls_matching(self, *prefix: str) -> List[dict]:
        return [c for c in self.calls() if c["argv"][: len(prefix)] == list(prefix)]

    def flag(self, name: str, content: str = "1") -> None:
        (self.ctl / name).write_text(content)

    def unflag(self, name: str) -> None:
        try:
            (self.ctl / name).unlink()
        except OSError:
            pass

    def set_scopes(
        self,
        granted: Sequence[str] = (),
        pending: Sequence[str] = (),
        config: bool = False,
        user: Sequence[str] = (),
    ) -> None:
        """Write the app's scope list as `GET /application/v6/scopes` replies with it.

        granted: tenant scopes with grant_status 1. pending: tenant scopes asked for but not
        granted yet (grant_status 2). config: the scope that lets the app add scopes itself.
        user: user-token scopes, which the code ignores. Without a call the scope call fails.
        """
        items = [{"grant_status": 1, "scope_name": s, "scope_type": "tenant"} for s in granted]
        items += [{"grant_status": 2, "scope_name": s, "scope_type": "tenant"} for s in pending]
        if config:
            items.append({"grant_status": 1, "scope_name": scope_link.APP_CONFIG_SCOPE, "scope_type": "tenant"})
        items += [{"grant_status": 1, "scope_name": s, "scope_type": "user"} for s in user]
        tmp = self.ctl / "scopes.json.tmp"
        tmp.write_text(json.dumps(items))
        os.replace(str(tmp), str(self.ctl / "scopes.json"))  # a running fake never reads half a file

    def scope_items(self) -> List[dict]:
        """The fake's scope list as it stands now (the fake changes it on PATCH and publish)."""
        return json.loads((self.ctl / "scopes.json").read_text())

    def store(self, bot: str = "lark-bot") -> Path:
        return self.home / ".lark-bots" / bot

    def mode(self, path: Path) -> int:
        return path.stat().st_mode & 0o777

    def configure(self, bot: str = "lark-bot", app_id: str = "cli_imp") -> None:
        """Quickly configure a store through import mode."""
        proc = self.run_onboard(["app", "--bot", bot, "--app-id", app_id], input="import-secret-value")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
