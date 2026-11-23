#!/usr/bin/env python3
"""Onboarding helper run by the Grok Bot itself.

The operator only has to scan one QR code. Every command prints short
``key: value`` lines and a final ``next:`` line that says what to do next, so an
LLM agent can drive the whole setup by following those lines.

Commands: app, wait, scopes, relay, webhook, routine. Run ``--help`` on any of them.
Target: Linux, python3 >= 3.9, standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))

import scope_link  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCOPES_FILE = ROOT / "scopes" / "bot-scopes.txt"
TEMPLATES_DIR = ROOT / "templates"
ROUTINE_TEMPLATES = {
    "handler": "handler-routine-prompt.md",
    "health-check": "health-check-routine-prompt.md",
}
DEFAULT_RELAY_REF = "~/.lark-relay"
RELAY_SRC = ROOT / "relay"

EVENT = "im.message.receive_v1"
DEFAULT_BOT = "lark-bot"
BOT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
URL_RE = re.compile(r"https?://\S+user_code=\S+")
QR_LINE_RE = re.compile("^[\\s▀-▟█#.■□░▒▓]*$")
MANAGED_KEYS = (
    "LARK_BOT_ENV",
    "LARK_BRAND",
    "LARK_BOT_APP_ID",
    "LARK_BOT_OPEN_ID",
    "LARK_BOT_NAME",
)
LOCAL_URL_RE = re.compile(r"^http://(127\.0\.0\.1|localhost)(:\d+)?(/\S*)?$")
WEBHOOK_TEST_TIMEOUT = 20
WEBHOOK_TEST_BODY = {
    "type": "relay_test",
    "summary": "grokbot-lark setup check",
    "instructions": "This is a connectivity check from grokbot-lark setup, not a message. "
    "Do nothing: send no reply and no chat message.",
}
BLOCKED_STATES = {"blocked", "failed", "fail", "missing", "error"}


class OnboardError(Exception):
    """An expected failure: printed as status/error/next, no traceback."""

    def __init__(self, message: str, next_step: Optional[str] = None, code: int = 1):
        super().__init__(message)
        self.message = message
        self.next_step = next_step
        self.code = code


# --------------------------------------------------------------------------
# small helpers


def out(*lines: str) -> None:
    for line in lines:
        print(line)
    sys.stdout.flush()


def one_line(text: str, limit: int = 300) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def poll_interval(default: float) -> float:
    try:
        return float(os.environ.get("ONBOARD_POLL_SECONDS", default))
    except ValueError:
        return default


def bot_name_arg(value: str) -> str:
    if not BOT_RE.match(value):
        raise argparse.ArgumentTypeError(
            "must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ (letters, digits, . _ -)"
        )
    return value


def env_name_arg(value: str) -> str:
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", value):
        raise argparse.ArgumentTypeError("must be an environment variable name")
    return value


def brand_arg(value: str) -> str:
    try:
        return scope_link.normalize_brand(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc))


def relay_dir_for(args: argparse.Namespace) -> Path:
    """--relay-dir, else ~/.lark-relay for the default bot and ~/.lark-relay-<bot> for any other."""
    if args.relay_dir:
        return Path(os.path.expanduser(args.relay_dir)).resolve()
    name = ".lark-relay" if args.bot == DEFAULT_BOT else f".lark-relay-{args.bot}"
    return (Path.home() / name).resolve()


def next_cmd(name: str, bot: str, extra: str = "") -> str:
    """A command line to print in next:. The default bot is left implicit, as in the docs."""
    bot_part = "" if bot == DEFAULT_BOT else f" --bot {bot}"
    return f"python3 scripts/onboard.py {name}{bot_part}" + (f" {extra}" if extra else "")


def parse_json(text: str) -> Any:
    """Return the first JSON object/array found in text, or None."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    idx = 0
    while True:
        starts = [i for i in (text.find("{", idx), text.find("[", idx)) if i != -1]
        if not starts:
            return None
        start = min(starts)
        try:
            obj, _ = decoder.raw_decode(text[start:])
            return obj
        except ValueError:
            idx = start + 1


def find_key(obj: Any, key: str, pred: Optional[Callable[[Any], bool]] = None) -> Any:
    """Depth-first search for the first value stored under `key`."""
    if isinstance(obj, dict):
        if key in obj and (pred is None or pred(obj[key])):
            return obj[key]
        for value in obj.values():
            found = find_key(value, key, pred)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = find_key(value, key, pred)
            if found is not None:
                return found
    return None


def read_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'").strip('"')
    return values


def write_private(path: Path, text: str) -> None:
    """Atomically write text to path with mode 600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def clean_key(value: str) -> str:
    value = (value or "").strip()
    value = re.sub(r"^authorization\s*:\s*", "", value, flags=re.I)
    value = re.sub(r"^bearer\s+", "", value, flags=re.I)
    return value.strip()


def last_meaningful_line(text: str) -> str:
    for raw in reversed((text or "").splitlines()):
        line = raw.strip()
        if not line or QR_LINE_RE.match(line) or "secret" in line.lower():
            continue
        return line
    return ""


# --------------------------------------------------------------------------
# the isolated store of one bot


class Store:
    def __init__(self, bot: str) -> None:
        self.bot = bot
        self.root = Path.home() / ".lark-bots" / bot
        self.config = self.root / "config"
        self.data = self.root / "data"
        self.logs = self.root / "logs"
        self.env_sh = self.root / "env.sh"
        self.app_init = self.root / "app-init.json"
        self.app_json = self.root / "app.json"

    def ensure(self) -> None:
        old = os.umask(0o077)
        try:
            for d in (self.config, self.data, self.logs):
                d.mkdir(parents=True, exist_ok=True)
            for d in (self.root.parent, self.root, self.config, self.data, self.logs):
                try:
                    os.chmod(d, 0o700)
                except OSError:
                    pass
        finally:
            os.umask(old)
        text = (
            f"# Isolated lark-cli store for {self.bot}. This file contains no secrets.\n"
            f"# Usage: source {self.env_sh} && lark-cli ... --as bot\n"
            'export PATH="${HOME}/.local/bin:${PATH}"\n'
            f'export LARKSUITE_CLI_CONFIG_DIR="{self.config}"\n'
            f'export LARKSUITE_CLI_DATA_DIR="{self.data}"\n'
            f'export LARKSUITE_CLI_LOG_DIR="{self.logs}"\n'
        )
        write_private(self.env_sh, text)

    def env(self) -> Dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("LARKSUITE_CLI_")}
        local_bin = str(Path.home() / ".local" / "bin")
        env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")
        env["LARKSUITE_CLI_CONFIG_DIR"] = str(self.config)
        env["LARKSUITE_CLI_DATA_DIR"] = str(self.data)
        env["LARKSUITE_CLI_LOG_DIR"] = str(self.logs)
        return env

    def cli_path(self) -> str:
        found = shutil.which("lark-cli", path=self.env()["PATH"])
        if not found:
            raise OnboardError(
                "lark-cli not found",
                "npm install -g @larksuite/cli, then run this command again",
            )
        return found

    def run(
        self,
        args: Sequence[str],
        stdin_text: Optional[str] = None,
        cwd: Optional[str] = None,
        timeout: float = 60,
    ) -> Tuple[int, str, str]:
        cli = self.cli_path()
        try:
            proc = subprocess.run(
                [cli] + list(args),
                input=stdin_text if stdin_text is not None else None,
                stdin=None if stdin_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                env=self.env(),
                cwd=cwd,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return 124, "", "lark-cli timed out"
        except OSError as exc:
            return 127, "", str(exc)
        return proc.returncode, proc.stdout or "", proc.stderr or ""

    # -- state files

    def read_json(self, path: Path) -> Optional[dict]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def write_json(self, path: Path, data: dict) -> None:
        write_private(path, json.dumps(data, indent=2, sort_keys=True) + "\n")

    # -- lark-cli queries

    def config_show(self) -> Optional[dict]:
        """Return {'appId', 'brand'} when an app is configured, else None."""
        rc, stdout, stderr = self.run(["config", "show"])
        obj = parse_json(stdout) or parse_json(stderr)
        if not isinstance(obj, dict):
            return None
        if obj.get("ok") is False or isinstance(obj.get("error"), dict):
            return None
        app_id = find_key(obj, "appId", lambda v: isinstance(v, str) and bool(v))
        if not app_id:
            return None
        brand = find_key(obj, "brand", lambda v: isinstance(v, str)) or "feishu"
        try:
            brand = scope_link.normalize_brand(brand)
        except ValueError:
            brand = "feishu"
        return {"appId": app_id, "brand": brand}

    def bot_info(self) -> Tuple[Optional[str], Optional[str]]:
        rc, stdout, _ = self.run(["api", "GET", "/open-apis/bot/v3/info", "--as", "bot"])
        if rc != 0:
            return None, None
        obj = parse_json(stdout)
        bot = find_key(obj, "bot", lambda v: isinstance(v, dict))
        if not bot:
            return None, None
        open_id = bot.get("open_id") if isinstance(bot.get("open_id"), str) else None
        name = bot.get("app_name") if isinstance(bot.get("app_name"), str) else None
        return open_id or None, name or None

    def make_qr(self, url: str, filename: str) -> Tuple[Optional[str], str]:
        size = 512 if len(url) < 300 else 1024
        try:
            tmp = tempfile.mkdtemp(prefix="grokbot-lark-", dir="/tmp")
        except OSError:
            tmp = tempfile.mkdtemp(prefix="grokbot-lark-")
        rc, stdout, stderr = self.run(
            ["auth", "qrcode", url, "--output", filename, "--size", str(size)], cwd=tmp
        )
        path = Path(tmp) / filename
        if rc == 0 and path.is_file():
            return str(path), ""
        shutil.rmtree(tmp, ignore_errors=True)
        reason = last_meaningful_line(stderr) or last_meaningful_line(stdout) or f"exit {rc}"
        return None, one_line(reason, 120)


# --------------------------------------------------------------------------
# registration process bookkeeping


def pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def registration_pending(state: Optional[dict]) -> bool:
    if not state:
        return False
    if Path(state.get("rc_file", "")).exists():
        return False
    return pid_alive(state.get("pid"))


def kill_registration(state: dict) -> None:
    pid = state.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return
        time.sleep(0.2)


def print_waiting(store: Store, state: dict) -> None:
    qr = state.get("qr_png")
    if qr and not Path(qr).is_file():
        qr, reason = store.make_qr(state.get("full_url") or state["url"], "app-qr.png")
        state["qr_png"] = qr
        state["qr_reason"] = reason
        store.write_json(store.app_init, state)
    out(
        "status: waiting_for_scan",
        f"bot: {store.bot}",
        f"app_name: {state.get('name', store.bot)}",
        f"qr_png: {qr or 'unavailable (' + state.get('qr_reason', 'not generated') + ')'}",
        f"qr_carries: {state.get('qr_carries', 'app name')}",
        f"verification_url: {state['url']}",
        "next: Send the operator ONE message now: attach the QR image (scanning it with Feishu or "
        "Lark creates the app with every permission in one go) and paste verification_url exactly "
        "as printed for anyone who cannot scan. Then immediately run: "
        f"{next_cmd('wait', store.bot)}",
    )


def print_ready(store: Store, info: dict) -> None:
    out(
        "status: ready",
        f"bot: {store.bot}",
        f"app_id: {info['app_id']}",
        f"brand: {info['brand']}",
        f"bot_open_id: {info.get('bot_open_id') or 'unknown'}",
        f"bot_name: {info.get('bot_name') or 'unknown'}",
        f"store_env: {store.env_sh}",
        f"next: {next_cmd('scopes', store.bot)}",
    )


def finalize(store: Store) -> dict:
    """Lock the store to the bot identity and record app.json (no secrets)."""
    rc, stdout, stderr = store.run(["config", "strict-mode", "bot", "--global"])
    if rc != 0:
        raise OnboardError(
            "strict-mode bot failed: " + one_line(last_meaningful_line(stderr + "\n" + stdout) or rc)
        )
    cfg = store.config_show()
    if not cfg:
        raise OnboardError(
            "no app is configured in this store",
            f"{next_cmd('app', store.bot)} (a new link is needed)",
        )
    rc, stdout, _ = store.run(["whoami"])
    identity = find_key(parse_json(stdout), "identity")
    if identity != "bot":
        raise OnboardError(
            f"lark-cli identity is {identity!r}, expected 'bot'; strict mode did not apply"
        )
    open_id, name = store.bot_info()
    previous = store.read_json(store.app_json) or {}
    if previous.get("app_id") == cfg["appId"]:
        open_id = open_id or previous.get("bot_open_id")
        name = name or previous.get("bot_name")
    info = {
        "app_id": cfg["appId"],
        "brand": cfg["brand"],
        "bot_open_id": open_id or "unknown",
        "bot_name": name or "unknown",
    }
    store.write_json(store.app_json, info)
    try:
        store.app_init.unlink()
    except OSError:
        pass
    return info


def load_app(store: Store) -> dict:
    """app.json, created lazily when the store is already configured."""
    info = store.read_json(store.app_json)
    if info and info.get("app_id"):
        return info
    if store.config_show():
        return finalize(store)
    raise OnboardError(
        "no app is configured for this bot",
        f"{next_cmd('app', store.bot)}",
    )


# --------------------------------------------------------------------------
# app


def decorate_url(
    url: str, name: str, desc: str, avatars: Sequence[str], scopes: Sequence[str], with_scopes: bool
) -> str:
    sep = "&" if "?" in url else "?"
    out_url = url + sep + "name=" + quote(name, safe="") + "&desc=" + quote(desc, safe="")
    for avatar in avatars:
        out_url += "&avatar=" + quote(avatar, safe="")
    if with_scopes:
        addons = scope_link.encode_addons(scope_link.build_addons(scopes, (EVENT,)))
        out_url += "&addons=" + quote(addons, safe="")
    return out_url + "&createOnly=true"


def cmd_app(args: argparse.Namespace) -> int:
    store = Store(args.bot)
    store.cli_path()
    store.ensure()
    if args.app_id:
        return app_import(store, args)

    state = store.read_json(store.app_init)
    if args.force and registration_pending(state):
        kill_registration(state or {})
        state = None
    if registration_pending(state):
        print_waiting(store, state or {})
        return 0
    if store.config_show() and not args.force:
        info = store.read_json(store.app_json)
        if not info or not info.get("app_id"):
            info = finalize(store)
        print_ready(store, info)
        return 0
    if args.force:
        for path in (store.app_json, store.app_init):
            try:
                path.unlink()
            except OSError:
                pass

    name = args.name or args.bot
    out_log = store.logs / "app-init.out"
    err_log = store.logs / "app-init.err"
    rc_file = store.logs / "app-init.rc"
    for path in (out_log, err_log, rc_file):
        if path.exists():
            path.unlink()
        if path != rc_file:
            path.touch(mode=0o600)

    env = store.env()
    env["RC"] = str(rc_file)
    with open(out_log, "ab") as fo, open(err_log, "ab") as fe:
        proc = subprocess.Popen(
            ["sh", "-c", '"$@"; echo $? > "$RC"', "sh", store.cli_path(),
             "config", "init", "--new", "--brand", args.brand],
            stdin=subprocess.DEVNULL,
            stdout=fo,
            stderr=fe,
            env=env,
            cwd=str(store.root),
            start_new_session=True,
        )

    base_url = ""
    deadline = time.time() + float(os.environ.get("ONBOARD_URL_TIMEOUT", 30))
    ended = False
    while True:
        match = URL_RE.search(err_log.read_text(encoding="utf-8", errors="replace"))
        if match:
            base_url = match.group(0)
            break
        if ended or time.time() >= deadline:
            break
        ended = proc.poll() is not None  # one more read after the process ends
        if not ended:
            time.sleep(0.1)
    if not base_url:
        detail = last_meaningful_line(err_log.read_text(encoding="utf-8", errors="replace"))
        if proc.poll() is None:
            kill_registration({"pid": proc.pid})
            detail = detail or "no verification URL appeared within the time limit"
        raise OnboardError(
            detail or "lark-cli ended without printing a verification URL",
            f"{next_cmd('app', args.bot)} (try again)",
        )

    scopes = scope_link.load_scopes(SCOPES_FILE)
    # The QR image carries the full link (scopes included); the link printed for pasting is the
    # same one without the long addons parameter, so it never depends on a model copying it.
    full_url = decorate_url(base_url, name, args.desc, args.avatar, scopes, not args.no_scopes)
    url = decorate_url(base_url, name, args.desc, args.avatar, scopes, False)
    qr, reason = store.make_qr(full_url, "app-qr.png")
    carries = "app name" if args.no_scopes else f"app name, {len(scopes)} bot scopes, {EVENT}"
    state = {
        "pid": proc.pid,
        "url": url,
        "full_url": full_url,
        "qr_png": qr,
        "qr_reason": reason,
        "out_log": str(out_log),
        "err_log": str(err_log),
        "rc_file": str(rc_file),
        "started_at": int(time.time()),
        "name": name,
        "brand": args.brand,
        "qr_carries": carries,
    }
    store.write_json(store.app_init, state)
    print_waiting(store, state)
    return 0


def app_import(store: Store, args: argparse.Namespace) -> int:
    existing = store.config_show()
    if existing and not args.force:
        if existing["appId"] != args.app_id:
            raise OnboardError(
                f"this store already holds app {existing['appId']}",
                f"{next_cmd('app', args.bot, '--app-id ' + args.app_id + ' --force')} "
                "(reads the secret from stdin) to replace it",
            )
        info = store.read_json(store.app_json) or finalize(store)
        print_ready(store, info)
        return 0
    if sys.stdin.isatty():
        out("status: error", "error: the app secret must be piped on stdin (it is never taken from argv)")
        return 2
    secret = sys.stdin.read().strip()
    if not secret:
        out("status: error", "error: empty app secret on stdin")
        return 2
    rc, stdout, stderr = store.run(
        ["config", "init", "--app-id", args.app_id, "--app-secret-stdin", "--brand", args.brand],
        stdin_text=secret,
    )
    if rc != 0:
        detail = (stderr + "\n" + stdout).replace(secret, "****")
        raise OnboardError("config init failed: " + one_line(last_meaningful_line(detail) or f"exit {rc}"))
    info = finalize(store)
    print_ready(store, info)
    return 0


# --------------------------------------------------------------------------
# wait


def failure_reason(state: dict, rc: str) -> str:
    texts = []
    for key in ("err_log", "out_log"):
        try:
            texts.append(Path(state.get(key, "")).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    blob = "\n".join(texts)
    low = blob.lower()
    if "expired" in low:
        return "expired"
    if "denied" in low or "declined" in low or "rejected" in low:
        return "denied"
    for raw in reversed(blob.splitlines()):
        obj = parse_json(raw.strip()) if raw.strip().startswith("{") else None
        message = find_key(obj, "message", lambda v: isinstance(v, str)) if obj else None
        if message and "secret" not in message.lower():
            return one_line(message)
    return one_line(last_meaningful_line(blob) or f"lark-cli exited with code {rc}")


def cmd_wait(args: argparse.Namespace) -> int:
    store = Store(args.bot)
    store.cli_path()
    store.ensure()
    state = store.read_json(store.app_init)
    if not state:
        if store.config_show():
            print_ready(store, finalize(store))
            return 0
        raise OnboardError(
            "no registration is in progress and no app is configured",
            next_cmd("app", args.bot),
        )
    rc_file = Path(state.get("rc_file", ""))
    deadline = time.time() + args.timeout
    interval = poll_interval(2)
    while True:
        if rc_file.exists():
            break
        if not pid_alive(state.get("pid")):
            time.sleep(0.3)  # the wrapper writes the rc file just before it exits
            if rc_file.exists():
                break
            out(
                "status: failed",
                "reason: the registration process ended without a result",
                f"next: {next_cmd('app', args.bot, '--force')} (a new link is needed)",
            )
            return 1
        if time.time() >= deadline:
            qr = state.get("qr_png")
            out(
                "status: waiting_for_scan",
                f"verification_url: {state['url']}",
                f"qr_png: {qr or 'unavailable (' + state.get('qr_reason', 'not generated') + ')'}",
                "next: The operator has not finished yet. Run the same wait command again.",
            )
            return 2
        time.sleep(max(0.0, min(interval, deadline - time.time())) or 0.01)
    rc = rc_file.read_text(encoding="utf-8").strip()
    if rc != "0":
        out(
            "status: failed",
            f"reason: {failure_reason(state, rc)}",
            f"next: {next_cmd('app', args.bot, '--force')} (a new link is needed)",
        )
        return 1
    print_ready(store, finalize(store))
    return 0


# --------------------------------------------------------------------------
# scopes


def bridge_state(store: Store) -> Tuple[str, List[str]]:
    rc, stdout, _ = store.run(
        ["event", "consume", EVENT, "--as", "bot", "--dry-run"], timeout=90
    )
    obj = parse_json(stdout)
    decision = find_key(obj, "decision", lambda v: isinstance(v, dict))
    if not decision:
        return "unknown", []
    status = str(decision.get("status", "")).lower()
    if status == "ready":
        return "yes", []
    if status != "blocked":
        return "unknown", []
    blockers = []
    for pre in decision.get("preconditions") or []:
        if not isinstance(pre, dict) or str(pre.get("status", "")).lower() not in BLOCKED_STATES:
            continue
        line = f"{pre.get('name', 'unknown')}: {one_line(pre.get('detail') or '')}"
        if pre.get("hint"):
            line += f" | {one_line(pre['hint'])}"
        blockers.append(line)
    return "no", blockers


def granted_tenant_scopes(store: Store, app_id: str) -> Optional[set]:
    rc, stdout, _ = store.run(
        ["api", "GET", f"/open-apis/application/v6/applications/{app_id}",
         "--params", '{"lang":"zh_cn"}', "--as", "bot"]
    )
    if rc != 0:
        return None
    obj = parse_json(stdout)
    if isinstance(obj, dict) and (obj.get("ok") is False or obj.get("code") not in (None, 0)):
        return None
    items = find_key(obj, "scopes", lambda v: isinstance(v, list))
    if items is None:
        return None
    granted = set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("scope"), str):
            continue
        types = item.get("token_types")
        if not types or "tenant" in types:
            granted.add(item["scope"])
    return granted


def scopes_report(store: Store, info: dict, required: List[str]) -> dict:
    bridge, blockers = bridge_state(store)
    granted = granted_tenant_scopes(store, info["app_id"])
    missing: Optional[List[str]] = None
    if granted is not None:
        missing = [s for s in required if s not in granted]
    if bridge == "no":
        status = "blocked"
    elif missing is None:
        status = "unknown"
    elif not missing:
        status = "complete"
    else:
        status = "partial"
    return {
        "status": status,
        "bridge": bridge,
        "blockers": blockers,
        "missing": missing,
        "granted_n": None if missing is None else len(required) - len(missing),
    }


def cmd_scopes(args: argparse.Namespace) -> int:
    store = Store(args.bot)
    store.cli_path()
    store.ensure()
    info = load_app(store)
    required = scope_link.load_scopes(SCOPES_FILE)
    deadline = time.time() + args.wait
    interval = poll_interval(10)
    while True:
        report = scopes_report(store, info, required)
        if report["status"] == "complete" or time.time() >= deadline:
            break
        time.sleep(max(0.0, min(interval, deadline - time.time())) or 0.01)

    status = report["status"]
    qr = None
    lines = [
        f"status: {status}",
        f"bridge_ready: {report['bridge']}",
        "granted: " + ("unknown" if report["granted_n"] is None else f"{report['granted_n']}/{len(required)}"),
    ]
    for blocker in report["blockers"]:
        lines.append(f"blocker: {blocker}")
    if status != "complete":
        targets = required if report["missing"] is None else report["missing"]
        if report["missing"]:
            lines.append("missing: " + ",".join(report["missing"]))
        apply_url = scope_link.launcher_url(
            info["app_id"], info["brand"], scope_link.build_addons(targets, (EVENT,))
        )
        qr, reason = store.make_qr(apply_url, "scopes-qr.png")
        if qr:
            lines += [
                f"apply_qr_png: {qr}",
                f"permissions_page: {scope_link.open_host(info['brand'])}/app/{info['app_id']}/auth",
            ]
        else:
            lines += [
                f"apply_url: {apply_url}",
                f"apply_qr_png: unavailable ({reason})",
                f"fallback_url: {scope_link.build_url(info['app_id'], info['brand'], targets)}",
                "fallback_note: after confirming on the fallback page, publish a new app version "
                "in the developer console",
            ]
    relay_cmd = next_cmd("relay", args.bot, "--start")
    if status == "complete":
        nxt = relay_cmd
    elif status == "blocked":
        send = (
            "apply_qr_png to scan (permissions_page is the manual alternative)"
            if qr
            else "apply_url exactly as printed"
        )
        nxt = (
            f"The relay cannot receive messages yet. Send the operator {send}, "
            f"then run: {next_cmd('scopes', args.bot, '--wait 600')}"
        )
    else:
        how = (
            "attach apply_qr_png once and say that scanning it adds the remaining permissions"
            if qr
            else "send apply_url exactly as printed"
        )
        nxt = f"Continue with: {relay_cmd}. In your final setup message, {how}; do not wait for it."
    lines.append(f"next: {nxt}")
    out(*lines)
    return 3 if status == "blocked" else 0


# --------------------------------------------------------------------------
# relay


def update_env_text(existing: str, values: Dict[str, Optional[str]]) -> str:
    """Replace managed keys in place, keep everything else, append missing ones."""
    lines: List[str] = []
    done = set()
    for raw in existing.splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=", raw)
        key = m.group(1) if m else None
        if key in values:
            if key in done:
                continue
            done.add(key)
            if values[key] is not None:
                lines.append(f"{key}={shlex.quote(values[key] or '')}")
            continue
        lines.append(raw)
    for key, value in values.items():
        if key not in done and value is not None:
            lines.append(f"{key}={shlex.quote(value)}")
    return "\n".join(lines) + "\n"


def install_relay(relay_dir: Path) -> bool:
    """Copy relay/*.py and relay/*.sh; return True when relay.py already existed."""
    sources = sorted(
        p for p in RELAY_SRC.iterdir() if p.is_file() and p.suffix in (".py", ".sh")
    ) if RELAY_SRC.is_dir() else []
    if not any(p.name == "relay.py" for p in sources):
        raise OnboardError(f"relay sources not found in {RELAY_SRC}")
    upgraded = (relay_dir / "relay.py").exists()
    relay_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(relay_dir, 0o700)
    for src in sources:
        dest = relay_dir / src.name
        tmp = relay_dir / (src.name + ".new")
        shutil.copyfile(str(src), str(tmp))
        os.chmod(tmp, 0o700 if src.suffix == ".sh" else 0o600)
        os.replace(tmp, dest)
    return upgraded


def webhook_values(relay_dir: Path) -> Tuple[str, str]:
    wh = read_env_file(relay_dir / "webhook.env")
    rl = read_env_file(relay_dir / "relay.env")
    url = key = ""
    for name in ("url", "key"):
        env_name = "LARK_BOT_WEBHOOK_" + name.upper()
        value = wh.get(env_name, "").strip() or rl.get(env_name, "").strip() or os.environ.get(env_name, "").strip()
        if name == "key":
            value = clean_key(value)
            key = value
        else:
            url = value
    return url, key


def run_script(path: Path, timeout: float = 90) -> Tuple[int, List[str]]:
    try:
        proc = subprocess.run(
            [str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, ["timed out"]
    except OSError as exc:
        return 127, [str(exc)]
    return proc.returncode, (proc.stdout or "").splitlines()


def cmd_relay(args: argparse.Namespace) -> int:
    store = Store(args.bot)
    store.cli_path()
    store.ensure()
    relay_dir = relay_dir_for(args)
    info = load_app(store)
    if not info.get("bot_open_id") or info["bot_open_id"] == "unknown":
        open_id, name = store.bot_info()
        if open_id:
            info["bot_open_id"] = open_id
            if name:
                info["bot_name"] = name
            store.write_json(store.app_json, info)
    open_id = info.get("bot_open_id")
    open_id = None if not open_id or open_id == "unknown" else open_id
    bot_name = info.get("bot_name")
    bot_name = None if not bot_name or bot_name == "unknown" else bot_name

    relay_env = relay_dir / "relay.env"
    try:
        existing = relay_env.read_text(encoding="utf-8")
    except OSError:
        existing = ""
    # One relay directory serves one bot. Installing a second bot into it would
    # make both answer through the same app.
    owner = read_env_file(relay_env).get("LARK_BOT_ENV", "").strip()
    if owner and Path(os.path.expandvars(os.path.expanduser(owner))) != store.env_sh:
        raise OnboardError(
            f"{relay_dir} already belongs to another bot ({owner})",
            f"{next_cmd('relay', args.bot, '--relay-dir ~/.lark-relay-' + args.bot)}"
            + (" --start" if args.start else ""),
        )
    upgraded = install_relay(relay_dir)
    if not existing.strip():
        existing = (
            "# Non-secret settings for the relay (written by scripts/onboard.py relay).\n"
            "# Loaded on every start; add your own KEY=value lines freely.\n"
        )
    write_private(
        relay_env,
        update_env_text(
            existing,
            {
                "LARK_BOT_ENV": str(store.env_sh),
                "LARK_BRAND": info["brand"],
                "LARK_BOT_APP_ID": info["app_id"],
                "LARK_BOT_OPEN_ID": open_id,
                "LARK_BOT_NAME": bot_name or store.bot,
            },
        ),
    )
    webhook_env = relay_dir / "webhook.env"
    if not webhook_env.exists():
        write_private(
            webhook_env,
            "# Handler routine webhook (secret; never print or commit). Set with scripts/onboard.py webhook.\n"
            "LARK_BOT_WEBHOOK_URL=\nLARK_BOT_WEBHOOK_KEY=\n",
        )
    url, key = webhook_values(relay_dir)
    configured = bool(url and key)

    out(
        f"status: {'upgraded' if upgraded else 'installed'}",
        f"bot: {store.bot}",
        f"relay_dir: {relay_dir}",
        f"relay_env: {relay_env}",
        f"bot_env: {store.env_sh}",
        f"bot_open_id: {open_id or 'unknown'}",
        f"webhook: {'configured' if configured else 'not configured'}",
    )
    code = 0
    if args.start:
        stop, start = relay_dir / "stop.sh", relay_dir / "start.sh"
        if not start.is_file():
            out(f"error: start.sh not found in {relay_dir}")
            code = 1
        else:
            stopped = True
            if stop.is_file():
                stop_rc, lines = run_script(stop)
                for line in lines:
                    out(f"relay: {line}")
                if stop_rc != 0:
                    # Starting now would only report "already running" on the old code.
                    out("error: the running relay did not stop, so it still runs the previous code")
                    stopped = False
                    code = 1
            if stopped:
                rc, lines = run_script(start)
                for line in lines:
                    out(f"relay: {line}")
                code = rc
    if configured:
        nxt = f"{relay_dir / 'status.sh'}"
    else:
        bot_flag = "" if args.bot == DEFAULT_BOT else f" --bot {args.bot}"
        flags = bot_flag
        if args.relay_dir:
            flags += f" --relay-dir {args.relay_dir}"
        nxt = (
            "Create the two routines; their instructions are printed by: "
            f"python3 scripts/onboard.py routine handler{bot_flag} and "
            f"python3 scripts/onboard.py routine health-check{bot_flag}. "
            "Then, in this order: 1) tell the operator where the webhook URL and key are "
            "(the handler routine's Webhook section), 2) request a secure secret named "
            f"LARK_BOT_WEBHOOK_KEY, 3) run: python3 scripts/onboard.py webhook{flags} "
            "--url '<POST to URL>'. Never put the key on the command line."
        )
    out(f"next: {nxt}")
    return code


# --------------------------------------------------------------------------
# webhook


def send_webhook_test(url: str, key: str) -> Tuple[str, str]:
    """POST one relay_test request. Returns (verdict, text for the test: line).

    verdict is "ok", "rejected" (wrong URL or key) or "failed". The text never
    contains the URL or the key.
    """
    request = urllib.request.Request(
        url,
        data=json.dumps(WEBHOOK_TEST_BODY).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=WEBHOOK_TEST_TIMEOUT) as response:
            code = response.getcode()
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
    except Exception as exc:  # noqa: BLE001 - the reason is the class name only
        return "failed", f"unreachable ({type(exc).__name__})"
    if 200 <= code < 300:
        return "ok", f"accepted (HTTP {code})"
    if code in (401, 403):
        return "rejected", f"rejected (HTTP {code})"
    return "failed", f"failed (HTTP {code})"


def cmd_webhook(args: argparse.Namespace) -> int:
    relay_dir = relay_dir_for(args)
    path = relay_dir / "webhook.env"
    current = read_env_file(path)

    url = (args.url or os.environ.get("LARK_BOT_WEBHOOK_URL") or current.get("LARK_BOT_WEBHOOK_URL") or "").strip()
    key = ""
    if args.key_stdin:
        if sys.stdin.isatty():
            out("status: error", "error: --key-stdin needs the key piped on stdin")
            return 2
        key = clean_key(sys.stdin.read())
    if not key:
        key = clean_key(os.environ.get(args.key_env) or current.get("LARK_BOT_WEBHOOK_KEY") or "")
    if url and not (url.startswith("https://") or LOCAL_URL_RE.match(url)):
        out("status: error", "error: the webhook URL must start with https://")
        return 2

    if not relay_dir.exists():
        relay_dir.mkdir(parents=True)
        os.chmod(relay_dir, 0o700)
    write_private(
        path,
        "# Handler routine webhook (secret; never print or commit).\n"
        f"LARK_BOT_WEBHOOK_URL={shlex.quote(url)}\n"
        f"LARK_BOT_WEBHOOK_KEY={shlex.quote(key)}\n",
    )
    status_sh = relay_dir / "status.sh"
    if url and key:
        skip = args.no_test or os.environ.get("ONBOARD_SKIP_WEBHOOK_TEST") == "1"
        verdict, detail = ("ok", "skipped") if skip else send_webhook_test(url, key)
        lines = ["url: set", "key: set", f"test: {detail}", f"file: {path}"]
        if verdict == "ok":
            out(
                "status: saved",
                *lines,
                "next: The relay picks this up within a few seconds; no restart needed. "
                f"Check: {status_sh}",
            )
            return 0
        if verdict == "rejected":
            nxt = (
                "The URL or key does not match. Ask the operator to copy both again from the "
                "routine's Webhook section (clicking a field copies it), save the key in the "
                "secret again, then run this command again."
            )
        else:
            nxt = (
                "The webhook did not accept the call. Check that the routine exists and is not "
                "paused, and that the URL is the routine's \"POST to\" value; then run this "
                "command again."
            )
        out("status: rejected", *lines, f"next: {nxt}")
        return 4
    missing = []
    if not url:
        missing.append("url (pass --url 'https://...')")
    if not key:
        missing.append(
            f"key (save it in a secure secret named {args.key_env} and run this again from a new "
            "shell, or pipe it with --key-stdin)"
        )
    out(
        "status: incomplete",
        f"url: {'set' if url else 'missing'}",
        f"key: {'set' if key else 'missing'}",
        f"file: {path}",
        "next: still missing " + "; ".join(missing),
    )
    return 3


# --------------------------------------------------------------------------
# routine


def routine_text(raw: str) -> str:
    """Everything after the first line that is exactly '---', trimmed; the whole text without one."""
    lines = raw.splitlines()
    if "---" in lines:
        lines = lines[lines.index("---") + 1:]
    return "\n".join(lines).strip("\n") + "\n"


def relay_ref(relay_dir: Path) -> str:
    """The directory as the routine text should name it: ~/... inside the home directory."""
    home = Path.home().resolve()
    try:
        return "~/" + relay_dir.relative_to(home).as_posix()
    except ValueError:
        return str(relay_dir)


def cmd_routine(args: argparse.Namespace) -> int:
    path = TEMPLATES_DIR / ROUTINE_TEMPLATES[args.kind]
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OnboardError(f"cannot read {path}: {exc.strerror or type(exc).__name__}")
    text = routine_text(raw)
    ref = relay_ref(relay_dir_for(args))
    if ref != DEFAULT_RELAY_REF:
        # Not inside a longer name such as ~/.lark-relay-other.
        text = re.sub(re.escape(DEFAULT_RELAY_REF) + r"(?![A-Za-z0-9_-])", lambda _m: ref, text)
    sys.stdout.write(text)
    sys.stdout.flush()
    return 0


# --------------------------------------------------------------------------
# CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="onboard.py",
        description="Set up a Grok Bot on Feishu/Lark. The operator scans one QR code; "
        "every command prints key: value lines and a final 'next:' line to follow.",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--bot", type=bot_name_arg, default=DEFAULT_BOT,
                       help="bot/store name (default: lark-bot); store is ~/.lark-bots/<bot>")

    p = sub.add_parser(
        "app",
        help="start creating the bot's app (QR flow) or import an existing one",
        description="Start creating the bot's Feishu/Lark app. Prints a verification URL and a QR "
        "image for the operator; then run 'wait'. With --app-id, imports an existing app "
        "instead (the app secret is read from stdin, never from argv).",
    )
    common(p)
    p.add_argument("--name", help="app name preset (default: the --bot value)")
    p.add_argument("--desc", default="Grok Bot", help="app description preset (default: Grok Bot)")
    p.add_argument("--avatar", action="append", default=[], metavar="URL",
                   help="public https image URL for the app avatar (repeatable, max 6)")
    p.add_argument("--brand", type=brand_arg, default="feishu",
                   help="feishu (China, default) or lark (international; aliases larksuite, international)")
    p.add_argument("--no-scopes", action="store_true", help="do not preset scopes and the message event")
    p.add_argument("--force", action="store_true", help="replace an already configured store")
    p.add_argument("--app-id", help="import mode: existing app id; the secret is read from stdin")
    p.set_defaults(func=cmd_app)

    p = sub.add_parser(
        "wait",
        help="block until the operator finished scanning, then finalize",
        description="Wait for the operator to finish on the verification page, then lock the store "
        "to the bot identity and record the app. Exit 0 ready, 2 still waiting, 1 failed.",
    )
    common(p)
    p.add_argument("--timeout", type=float, default=600, help="seconds to wait (default: 600)")
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser(
        "scopes",
        help="show granted vs missing scopes and whether the bridge is ready",
        description="Check which required bot scopes are granted and whether the message bridge is "
        "ready. Exit 0 complete/partial/unknown, 3 blocked.",
    )
    common(p)
    p.add_argument("--wait", type=float, default=0, metavar="SECONDS",
                   help="re-check every 10 s until complete or the time is up (default: 0, check once)")
    p.set_defaults(func=cmd_scopes)

    p = sub.add_parser(
        "relay",
        help="install or upgrade the relay and write its settings",
        description="Copy the relay into the relay directory and write relay.env (non-secret "
        "settings) and an empty webhook.env if missing.",
    )
    common(p)
    p.add_argument("--relay-dir", default=None,
                   help="relay directory (default: ~/.lark-relay; ~/.lark-relay-<bot> when --bot is given)")
    p.add_argument("--start", action="store_true", help="restart the relay after installing")
    p.set_defaults(func=cmd_relay)

    p = sub.add_parser(
        "webhook",
        help="save the handler routine's webhook URL and key",
        description="Write webhook.env (mode 600). The key is never taken from argv: it is read "
        "from the environment variable named by --key-env, or from stdin with --key-stdin. "
        "After saving it sends one connectivity check (skip with --no-test). "
        "Exit 0 saved, 3 incomplete, 4 the check failed.",
    )
    common(p)
    p.add_argument("--relay-dir", default=None,
                   help="relay directory (default: ~/.lark-relay; ~/.lark-relay-<bot> when --bot is given)")
    p.add_argument("--url", help="webhook URL (https://...); falls back to env LARK_BOT_WEBHOOK_URL, then the file")
    p.add_argument("--key-stdin", action="store_true", help="read the key from stdin")
    p.add_argument("--key-env", default="LARK_BOT_WEBHOOK_KEY", metavar="NAME", type=env_name_arg,
                   help="environment variable that holds the key (default: LARK_BOT_WEBHOOK_KEY); "
                   "give each bot on a shared machine its own name")
    p.add_argument("--no-test", action="store_true", help="do not send the connectivity check")
    p.set_defaults(func=cmd_webhook)

    p = sub.add_parser(
        "routine",
        help="print the instruction text of a routine",
        description="Print only the instruction of the handler or health-check routine, ready to "
        "redirect to a file or paste. Paths in it follow this bot's relay directory. Needs no "
        "lark-cli and no app.",
    )
    common(p)
    p.add_argument("kind", choices=sorted(ROUTINE_TEMPLATES), help="which routine")
    p.add_argument("--relay-dir", default=None,
                   help="relay directory (default: ~/.lark-relay; ~/.lark-relay-<bot> when --bot is given)")
    p.set_defaults(func=cmd_routine)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "command", None) == "app":
        if len(args.avatar) > 6:
            out("status: error", "error: at most 6 --avatar values are allowed")
            return 2
        bad = [a for a in args.avatar if not a.startswith("https://")]
        if bad:
            out("status: error", "error: --avatar values must start with https://")
            return 2
    try:
        return args.func(args)
    except OnboardError as exc:
        out("status: error", f"error: {one_line(exc.message)}")
        if exc.next_step:
            out(f"next: {exc.next_step}")
        return exc.code
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - last resort, keep output agent-friendly
        out("status: error", f"error: {one_line(f'{type(exc).__name__}: {exc}')}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
