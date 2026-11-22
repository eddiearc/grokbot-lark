#!/usr/bin/env python3
"""Fake lark-cli for an end-to-end rehearsal. Controlled by files in $FAKE_CTRL."""
import json
import os
import sys
import time
from pathlib import Path

ctrl = Path(os.environ["FAKE_CTRL"])
args = sys.argv[1:]
cfg_dir = Path(os.environ.get("LARKSUITE_CLI_CONFIG_DIR", str(Path.home() / ".lark-cli")))
marker = cfg_dir / "configured"
with (ctrl / "calls.log").open("a") as f:
    f.write(json.dumps({"args": args, "config_dir": str(cfg_dir)}) + "\n")


def emit(obj):
    print(json.dumps(obj), flush=True)


def has(*words):
    return all(w in args for w in words)


if args[:3] == ["config", "init", "--new"]:
    sys.stderr.write("█▀▀█ ▀▀ █\n  https://open.feishu.cn/page/cli?user_code=TEST-CODE&lpv=1.0.100&ocv=1.0.100&from=cli\nwaiting...\n")
    sys.stderr.flush()
    while not (ctrl / "scan-done").exists():
        time.sleep(0.2)
    cfg_dir.mkdir(parents=True, exist_ok=True)
    marker.write_text("cli_fake")
    emit({"appId": "cli_fake", "appSecret": "****", "brand": "feishu"})
    sys.exit(0)
if args[:2] == ["config", "show"]:
    if marker.exists():
        emit({"appId": "cli_fake", "appSecret": "****", "brand": "feishu"})
    else:
        emit({"ok": False, "error": {"type": "config", "subtype": "not_configured", "message": "not configured"}})
    sys.exit(0)
if args[:2] == ["config", "strict-mode"]:
    sys.exit(0)
if args[:1] == ["whoami"]:
    emit({"appId": "cli_fake", "brand": "feishu", "identity": "bot", "identitySource": "strict_mode", "available": True})
    sys.exit(0)
if args[:2] == ["auth", "qrcode"]:
    out = args[args.index("--output") + 1]
    Path(out).write_bytes(b"\x89PNG fake")
    emit({"file_path": str(Path(out).resolve()), "ok": True})
    sys.exit(0)
if args[:2] == ["api", "GET"]:
    path = args[2]
    if path.endswith("/bot/v3/info"):
        emit({"ok": True, "data": {"bot": {"open_id": "ou_bot", "app_name": "Atlas"}}})
    elif "/applications/" in path:
        scopes = [l.strip() for l in Path(os.environ["FAKE_SCOPES"]).read_text().splitlines() if l.strip() and not l.startswith("#")]
        emit({"ok": True, "data": {"app": {"scopes": [{"scope": s, "token_types": ["tenant"]} for s in scopes]}}})
    else:
        emit({"ok": False, "error": {"type": "api", "subtype": "unknown", "message": "unexpected path"}})
    sys.exit(0)
if args[:2] == ["event", "status"]:
    emit({"apps": [{"app_id": "cli_fake", "running": True, "status": "running",
                    "consumers": [{"pid": 1, "event_key": "im.message.receive_v1", "received": 1, "dropped": 0}]}]})
    sys.exit(0)
if args[:2] == ["event", "consume"]:
    if "--dry-run" in args:
        emit({"ok": True, "dry_run": True, "data": {"decision": {"status": "ready", "preconditions": []}}})
        sys.exit(0)
    sys.stderr.write("[event] ready event_key=im.message.receive_v1\n")
    sys.stderr.flush()
    time.sleep(1.0)
    emit({"type": "im.message.receive_v1", "message_id": "om_1", "id": "om_1", "chat_id": "oc_1",
          "chat_type": "p2p", "sender_id": "ou_user", "sender_type": "user", "message_type": "text",
          "content": "hello bot", "mentions": [], "create_time": str(int(time.time() * 1000))})
    sys.stdin.read()  # block until the relay closes our stdin
    sys.stderr.write("[event] exited (reason: signal)\n")
    sys.exit(0)
if args[:3] == ["im", "reactions", "create"]:
    emit({"ok": True, "data": {"reaction_id": "r_1"}})
    sys.exit(0)
if args[:3] == ["im", "reactions", "delete"]:
    (ctrl / "reaction-deleted").write_text("1")
    emit({"ok": True, "data": {}})
    sys.exit(0)
if args[:3] == ["im", "reactions", "list"]:
    emit({"ok": True, "data": {"items": [{"reaction_id": "r_1"}]}})
    sys.exit(0)
if args[:2] == ["contact", "+get-user"]:
    emit({"ok": True, "data": {"user": {"open_id": "ou_user", "name": "Alice", "email": "alice@example.com"}}})
    sys.exit(0)
if args[:2] == ["im", "+messages-mget"]:
    emit({"ok": True, "data": {"messages": [{"message_id": "om_1", "sender": {"id": "ou_user", "id_type": "open_id", "sender_type": "user", "name": "Alice"}}]}})
    sys.exit(0)
if args[:3] == ["im", "chats", "get"]:
    emit({"ok": True, "data": {"chat_mode": "p2p"}})
    sys.exit(0)
if args[:2] == ["im", "+chat-messages-list"]:
    msgs = [{"message_id": "om_1", "sender": {"id": "ou_user", "sender_type": "user"}, "create_time": time.strftime("%Y-%m-%d %H:%M")}]
    if (ctrl / "replied").exists():
        msgs.insert(0, {"message_id": "om_2", "reply_to": "om_1", "sender": {"id": "cli_fake", "id_type": "app_id", "sender_type": "app"},
                        "create_time": time.strftime("%Y-%m-%d %H:%M")})
    emit({"ok": True, "data": {"messages": msgs, "has_more": False}})
    sys.exit(0)
emit({"ok": False, "error": {"type": "validation", "subtype": "invalid_argument", "message": "fake: unhandled " + " ".join(args)}})
sys.exit(2)
