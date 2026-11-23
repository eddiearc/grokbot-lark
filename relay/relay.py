#!/usr/bin/env python3
"""Inbound Feishu relay: consume im.message.receive_v1 as bot and POST to Grok webhook."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from identity import IdentityResolver, build_payload, fit_payload
from lifecycle import POLL_INTERVAL, Lifecycle
from store import Store

BASE = Path(__file__).resolve().parent
LOG_DIR = BASE / "logs"
STATE_DIR = BASE / "state"
SPOOL_DIR = BASE / "spool"
WEBHOOK_ENV = BASE / "webhook.env"
SEEN_PATH = STATE_DIR / "seen_message_ids.json"
SPOOL_PATH = SPOOL_DIR / "pending.jsonl"
STATE_DB = BASE / "state.db"
BOT_OPEN_ID = os.environ.get("LARK_BOT_OPEN_ID", "").strip()
CONSUME_CMD = [
    "lark-cli",
    "event",
    "consume",
    "im.message.receive_v1",
    "--as",
    "bot",
]
SEEN_MAX = 50000
POST_TIMEOUT = 20
POST_RETRIES = 3
FLUSH_INTERVAL_SEC = 2.0
# flush_spool runs in both the main loop and the reader thread; append_spool in
# the reader thread. One lock keeps a rewrite from overwriting a concurrent append.
SPOOL_LOCK = threading.RLock()

for d in (LOG_DIR, STATE_DIR, SPOOL_DIR):
    d.mkdir(mode=0o700, exist_ok=True)


def log(msg: str, **fields: Any) -> None:
    rec = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "msg": msg}
    for k, v in fields.items():
        if k.lower() in {"key", "secret", "token", "authorization", "webhook_key", "password"}:
            continue
        rec[k] = v
    line = json.dumps(rec, ensure_ascii=False, default=str)
    print(line, flush=True)
    with (LOG_DIR / "relay.jsonl").open("a", encoding="utf-8") as f:
        f.write(line + "\n")


BEARER_PREFIX = re.compile(r"^(authorization:\s*)?bearer\s+", re.IGNORECASE)


def _clean_key(value: str) -> str:
    return BEARER_PREFIX.sub("", value.strip()).strip()


def load_webhook() -> tuple[str, str] | None:
    """Return (url, key) if both are non-empty; never log values.

    webhook.env wins; the environment variables of the same names fill in any
    value it leaves empty.
    """
    url = key = ""
    if WEBHOOK_ENV.is_file():
        try:
            text = WEBHOOK_ENV.read_text(encoding="utf-8")
        except OSError as e:
            log("webhook_env_unreadable", error=str(e))
            text = ""
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip("'").strip('"')
            if k == "LARK_BOT_WEBHOOK_URL":
                url = v
            elif k == "LARK_BOT_WEBHOOK_KEY":
                key = v
    url = url.strip() or os.environ.get("LARK_BOT_WEBHOOK_URL", "").strip()
    key = _clean_key(key) or _clean_key(os.environ.get("LARK_BOT_WEBHOOK_KEY", ""))
    if url and key:
        return url, key
    return None


class SeenStore:
    def __init__(self, path: Path, limit: int) -> None:
        self.path = path
        self.limit = limit
        self._lock = threading.Lock()
        self._ids: OrderedDict[str, None] = OrderedDict()
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            ids = data if isinstance(data, list) else data.get("ids", [])
            for mid in ids[-self.limit :]:
                if isinstance(mid, str) and mid:
                    self._ids[mid] = None
        except Exception as e:
            log("seen_store_load_failed", error=type(e).__name__)

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ids": list(self._ids.keys())}), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(self.path)

    def contains(self, mid: str) -> bool:
        with self._lock:
            return mid in self._ids

    def add(self, mid: str) -> None:
        if not mid:
            return
        with self._lock:
            self._ids.pop(mid, None)
            self._ids[mid] = None
            while len(self._ids) > self.limit:
                self._ids.popitem(last=False)
            self._save()


seen = SeenStore(SEEN_PATH, SEEN_MAX)
stop_event = threading.Event()
proc: subprocess.Popen[bytes] | None = None
store: Store | None = None
life: Lifecycle | None = None
resolver: IdentityResolver | None = None


def mention_ids(mentions: Any) -> set[str]:
    ids: set[str] = set()
    if not isinstance(mentions, list):
        return ids
    for m in mentions:
        if isinstance(m, str) and m:
            ids.add(m)
            continue
        if not isinstance(m, dict):
            continue
        for k in ("id", "open_id", "openId"):
            v = m.get(k)
            if isinstance(v, str) and v:
                ids.add(v)
        ident = m.get("id")
        if isinstance(ident, dict):
            for k in ("open_id", "openId", "id"):
                v = ident.get(k)
                if isinstance(v, str) and v:
                    ids.add(v)
    return ids


def should_forward(event: dict[str, Any]) -> tuple[bool, str]:
    mid = str(event.get("message_id") or event.get("id") or "").strip()
    if not mid:
        return False, "missing_message_id"
    if seen.contains(mid):
        return False, "duplicate"
    sender_id = str(event.get("sender_id") or "").strip()
    sender_type = str(event.get("sender_type") or "").strip().lower()
    if sender_id and sender_id == BOT_OPEN_ID:
        return False, "self"
    if sender_type == "bot":
        return False, "self"
    chat_type = str(event.get("chat_type") or "").strip().lower()
    mentions = event.get("mentions") or []
    if chat_type == "p2p":
        return True, "p2p"
    ids = mention_ids(mentions)
    if BOT_OPEN_ID and BOT_OPEN_ID in ids:
        return True, "group_mention"
    return False, "group_no_mention"


def list_chat_messages(chat_id: str, page_size: int = 50) -> list[dict[str, Any]]:
    from cliutil import run_cli_retry

    if not chat_id:
        return []
    env = run_cli_retry(
        [
            "im",
            "+chat-messages-list",
            "--chat-id",
            chat_id,
            "--order",
            "desc",
            "--page-size",
            str(page_size),
            "--no-reactions",
            "--json",
        ],
        timeout=25,
        log=log,
        op="chat_messages_list",
    )
    if not env.get("ok"):
        return []
    data = env.get("data") or env
    msgs = data.get("messages") if isinstance(data, dict) else None
    return [m for m in msgs if isinstance(m, dict)] if isinstance(msgs, list) else []


def poll_completions() -> None:
    if life is None:
        return
    chats: dict[str, str | None] = {}
    for job in store.active_jobs():
        cid = job.get("chat_id")
        if cid:
            chats[cid] = job.get("chat_type")
    for chat_id, chat_type in chats.items():
        try:
            msgs = list_chat_messages(chat_id)
            life.apply_listed_messages(chat_id, msgs, chat_type)
        except Exception as e:
            log("poll_chat_failed", chat_id=chat_id, error=type(e).__name__)
    life.mark_timeouts()
    life.abandon_stale()


def post_webhook(url: str, key: str, body: dict[str, Any]) -> bool:
    data = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
    headers = {
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
    }
    last_err = ""
    for attempt in range(1, POST_RETRIES + 1):
        try:
            req = Request(url, data=data, headers=headers, method="POST")
            with urlopen(req, timeout=POST_TIMEOUT) as resp:
                code = getattr(resp, "status", None) or resp.getcode()
                resp.read()
            if 200 <= int(code) < 300:
                return True
            last_err = f"http_{code}"
        except HTTPError as e:
            last_err = f"http_{e.code}"
            try:
                e.read()
            except Exception:
                pass
        except URLError as e:
            last_err = type(e.reason).__name__ if e.reason else "url_error"
        except Exception as e:
            last_err = type(e).__name__
        time.sleep(min(2 ** (attempt - 1), 8))
    log("webhook_post_failed", message_id=body.get("message_id"), error=last_err)
    return False


def append_spool(body: dict[str, Any]) -> None:
    with SPOOL_LOCK:
        SPOOL_PATH.parent.mkdir(mode=0o700, exist_ok=True)
        line = json.dumps(body, ensure_ascii=False, default=str)
        with SPOOL_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        try:
            SPOOL_PATH.chmod(0o600)
        except OSError:
            pass


def flush_spool() -> None:
    with SPOOL_LOCK:
        _flush_spool_locked()


def _flush_spool_locked() -> None:
    creds = load_webhook()
    if not creds or not SPOOL_PATH.is_file():
        return
    url, key = creds
    try:
        lines = SPOOL_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    items = [ln for ln in lines if ln.strip()]
    if not items:
        return
    remaining: list[str] = []
    flushed = 0
    for idx, line in enumerate(items):
        try:
            body = json.loads(line)
        except json.JSONDecodeError:
            log("spool_bad_line")
            continue
        if post_webhook(url, key, body):
            flushed += 1
            continue
        remaining = items[idx:]
        break
    if remaining == items:
        # Nothing was posted and nothing was dropped: leave the file (and its
        # modification time, which status.sh reads) untouched.
        return
    tmp = SPOOL_PATH.with_suffix(".tmp")
    if remaining:
        tmp.write_text("\n".join(remaining) + "\n", encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(SPOOL_PATH)
    else:
        SPOOL_PATH.write_text("", encoding="utf-8")
        SPOOL_PATH.chmod(0o600)
        if tmp.exists():
            tmp.unlink()
    if flushed:
        log("spool_flushed", count=flushed, remaining=len(remaining))


def deliver(body: dict[str, Any]) -> bool:
    mid = str(body.get("message_id") or "")
    creds = load_webhook()
    if creds:
        url, key = creds
        flush_spool()
        if post_webhook(url, key, body):
            seen.add(mid)
            log(
                "forwarded",
                message_id=mid,
                chat_id=body.get("chat_id"),
                chat_type=body.get("chat_type"),
            )
            return True
        append_spool(body)
        seen.add(mid)
        log("spooled_after_post_failure", message_id=mid)
        return False
    append_spool(body)
    seen.add(mid)
    log(
        "spooled_webhook_unconfigured",
        message_id=mid,
        chat_id=body.get("chat_id"),
        chat_type=body.get("chat_type"),
    )
    return False


def handle_event(event: dict[str, Any]) -> None:
    mid = str(event.get("message_id") or event.get("id") or "").strip()
    # Bot's own messages: never forward; use them for completion if they ever arrive.
    if life is not None:
        sender_id = str(event.get("sender_id") or "").strip()
        sender_type = str(event.get("sender_type") or "").strip().lower()
        if (sender_id and sender_id == BOT_OPEN_ID) or sender_type in {"bot", "app"}:
            life.handle_possible_bot_event(event)
            if mid:
                seen.add(mid)
            log("skipped", reason="self", message_id=mid, chat_type=event.get("chat_type"))
            return
    ok, reason = should_forward(event)
    if not ok:
        if reason in {"self", "duplicate", "group_no_mention", "missing_message_id"}:
            log("skipped", reason=reason, message_id=mid, chat_type=event.get("chat_type"))
        if reason not in {"duplicate", "missing_message_id"} and mid:
            seen.add(str(mid))
        return
    chat_id = str(event.get("chat_id") or "")
    chat_type = str(event.get("chat_type") or "")
    reaction_id = None
    if life is not None and mid and chat_id:
        job = life.start_working(mid, chat_id, chat_type)
        reaction_id = (job or {}).get("reaction_id")
    if resolver is not None:
        body = fit_payload(build_payload(event, resolver, reaction_id=reaction_id))
    else:
        content = event.get("content")
        body = {
            "message_id": mid,
            "chat_id": chat_id,
            "chat_type": chat_type,
            "sender_open_id": event.get("sender_id"),
            "text": content if isinstance(content, str) else "",
            "mentions": event.get("mentions") if isinstance(event.get("mentions"), list) else [],
            "raw": event,
        }
        if reaction_id:
            body["reaction_id"] = reaction_id
    ok_deliver = deliver(body)
    if not ok_deliver and life is not None and mid:
        life.mark_webhook_failed(mid)


def stderr_reader(pipe, ready: threading.Event) -> None:
    assert pipe is not None
    err_log = LOG_DIR / "consume.stderr.log"
    try:
        for raw in iter(pipe.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            low = line.lower()
            redacted = line
            if "bearer " in low or "app_secret" in low or "client_secret" in low:
                redacted = "[redacted-stderr-line]"
            with err_log.open("a", encoding="utf-8") as f:
                f.write(redacted + "\n")
            if "[event] ready" in line:
                ready.set()
                log("consumer_ready", event_key="im.message.receive_v1")
            elif "[event] exited" in line:
                log("consumer_exited_marker", detail=redacted)
            elif line.startswith("{") and '"ok":false' in line.replace(" ", ""):
                log("consumer_error_envelope")
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def stdout_reader(pipe) -> None:
    assert pipe is not None
    try:
        for raw in iter(pipe.readline, b""):
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                log("bad_ndjson")
                continue
            if not isinstance(event, dict):
                log("non_object_event")
                continue
            try:
                handle_event(event)
            except Exception as e:
                log("handle_event_failed", error=type(e).__name__)
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def handle_signal(signum, _frame) -> None:
    log("signal", signum=signum)
    stop_event.set()
    if proc and proc.poll() is None:
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.send_signal(signal.SIGTERM)
        except Exception:
            pass


def main() -> int:
    global proc
    from cliutil import _env as _lark_cli_env

    os.environ.setdefault("PATH", "")
    for key, val in _lark_cli_env().items():
        if key in {
            "PATH",
            "LARKSUITE_CLI_CONFIG_DIR",
            "LARKSUITE_CLI_DATA_DIR",
            "LARKSUITE_CLI_LOG_DIR",
        }:
            os.environ[key] = val

    if not shutil.which("lark-cli"):
        log("missing_lark_cli")
        return 2

    global store, life, resolver
    store = Store(STATE_DB)
    life = Lifecycle(store, log=log)
    resolver = IdentityResolver(store, log=log)

    creds = load_webhook()
    log(
        "relay_start",
        webhook_configured=bool(creds),
        bot_open_id=BOT_OPEN_ID,
        consume="im.message.receive_v1",
    )
    if creds:
        flush_spool()
    try:
        life.reconcile()
        poll_completions()
    except Exception as e:
        log("startup_reconcile_failed", error=type(e).__name__)

    env = os.environ.copy()
    ready = threading.Event()
    proc = subprocess.Popen(
        CONSUME_CMD,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        bufsize=0,
    )
    t_err = threading.Thread(target=stderr_reader, args=(proc.stderr, ready), daemon=True)
    t_out = threading.Thread(target=stdout_reader, args=(proc.stdout,), daemon=True)
    t_err.start()
    t_out.start()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    deadline = time.time() + 60
    while not ready.is_set() and proc.poll() is None and not stop_event.is_set():
        remaining = deadline - time.time()
        if remaining <= 0:
            log("consumer_ready_timeout")
            handle_signal(signal.SIGTERM, None)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.terminate()
            return 4
        ready.wait(timeout=min(1.0, remaining))

    if proc.poll() is not None and not ready.is_set():
        log("consumer_exited_before_ready", returncode=proc.returncode)
        return proc.returncode or 4

    last_flush = time.time()
    last_poll = 0.0
    while not stop_event.is_set():
        rc = proc.poll()
        if rc is not None:
            t_out.join(timeout=5)
            t_err.join(timeout=5)
            log("consumer_process_exited", returncode=rc)
            return rc if rc != 0 else 1
        now = time.time()
        if now - last_flush >= FLUSH_INTERVAL_SEC:
            try:
                flush_spool()
            except Exception as e:
                log("flush_failed", error=type(e).__name__)
            last_flush = now
        if now - last_poll >= POLL_INTERVAL:
            try:
                poll_completions()
            except Exception as e:
                log("poll_failed", error=type(e).__name__)
            last_poll = now
        stop_event.wait(timeout=0.5)

    handle_signal(signal.SIGTERM, None)
    try:
        rc = proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.terminate()
        rc = proc.wait(timeout=10)
    t_out.join(timeout=5)
    t_err.join(timeout=5)
    log("relay_stop", returncode=rc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
