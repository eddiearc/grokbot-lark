#!/usr/bin/env python3
"""Run lark-cli as bot and parse JSON envelopes."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent
HOME_BIN = str(Path.home() / ".local" / "bin")


def _env() -> dict[str, str]:
    env = os.environ.copy()
    path = env.get("PATH", "")
    if HOME_BIN not in path.split(":"):
        env["PATH"] = HOME_BIN + ":" + path
    return env


def parse_json_stdout(stdout: str) -> dict[str, Any] | None:
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def find_field(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = find_field(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_field(item, key)
            if found is not None:
                return found
    return None


def _lark_cli() -> str:
    home = Path.home() / ".local" / "bin" / "lark-cli"
    if home.is_file():
        return str(home)
    return "lark-cli"


def _with_as_bot(args: list[str]) -> list[str]:
    if "--as" in args or any(str(a).startswith("--as=") for a in args):
        return list(args)
    return [*args, "--as", "bot"]


def run_cli(args: list[str], timeout: float = 20) -> dict[str, Any]:
    """Run lark-cli ... --as bot and return the parsed JSON object (may include ok/error)."""
    cmd = [_lark_cli(), *_with_as_bot(args)]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            env=_env(),
            cwd=str(BASE),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": {"message": "timeout", "subtype": "timeout"}}
    except Exception as e:
        return {"ok": False, "error": {"message": type(e).__name__}}
    parsed = parse_json_stdout(proc.stdout.decode("utf-8", errors="replace"))
    if parsed is None:
        err = proc.stderr.decode("utf-8", errors="replace")
        envelope = parse_json_stdout(err)
        if envelope is not None:
            return envelope
        return {
            "ok": False,
            "error": {
                "message": "non_json_output",
                "returncode": proc.returncode,
            },
        }
    if "ok" not in parsed:
        parsed = {"ok": proc.returncode == 0, "data": parsed}
    return parsed


def run_cli_retry(
    args: list[str],
    timeout: float = 20,
    attempts: int = 4,
    log=None,
    op: str = "cli",
) -> dict[str, Any]:
    last: dict[str, Any] = {"ok": False}
    delay = 0.5
    for i in range(1, attempts + 1):
        last = run_cli(args, timeout=timeout)
        if last.get("ok"):
            return last
        err = last.get("error") or {}
        subtype = str(err.get("subtype") or "")
        # Permission / validation errors will not recover on retry.
        if subtype in {"missing_scope", "invalid_argument", "permission_denied"}:
            break
        if log:
            log("cli_retry", op=op, attempt=i, error=subtype or err.get("message"))
        if i < attempts:
            time.sleep(delay)
            delay = min(delay * 2, 8)
    return last
