#!/usr/bin/env python3
"""Build a one-click bot (tenant) scope-apply URL from scopes/bot-scopes.txt."""

from __future__ import annotations

import argparse
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCOPES = ROOT / "scopes" / "bot-scopes.txt"

ENDPOINTS = {
    "feishu": "https://open.feishu.cn/page/scope-apply",
    "lark": "https://open.larksuite.com/page/scope-apply",
    "larksuite": "https://open.larksuite.com/page/scope-apply",
}


def load_scopes(path: Path) -> list[str]:
    scopes: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        scopes.append(line)
    return scopes


def build_url(app_id: str, brand: str, scopes: list[str]) -> str:
    base = ENDPOINTS[brand]
    encoded = quote(",".join(scopes), safe="")
    return f"{base}?clientID={app_id}&scopes={encoded}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Print a one-click URL that applies all default bot (tenant) scopes."
    )
    parser.add_argument(
        "--app-id",
        required=True,
        help="App ID (client ID) from the Feishu or Lark developer console",
    )
    parser.add_argument(
        "--brand",
        choices=sorted(ENDPOINTS),
        default="feishu",
        help="feishu for China (open.feishu.cn); lark or larksuite for international",
    )
    parser.add_argument("--scopes-file", type=Path, default=DEFAULT_SCOPES)
    args = parser.parse_args()
    scopes = load_scopes(args.scopes_file)
    if not scopes:
        raise SystemExit(f"no scopes found in {args.scopes_file}")
    print(build_url(args.app_id, args.brand, scopes))


if __name__ == "__main__":
    main()
