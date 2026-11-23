#!/usr/bin/env python3
"""Build bot (tenant) scope links from scopes/bot-scopes.txt.

``onboard.py`` does not need these long links: it asks for one scope
(``APP_CONFIG_SCOPE``) and then adds the rest through the API. This command is
for doing it by hand. Two link styles exist:

* classic: ``/page/scope-apply`` -- the operator confirms on a plain page.
  Scopes that need review take effect after a new app version is published.
* launcher (``--launcher``): ``/page/launcher`` with ``addons`` -- the page shows
  the difference and publishes the version by itself (when the tenant supports it).

The helpers in this module are imported by ``onboard.py``.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCOPES = ROOT / "scopes" / "bot-scopes.txt"
DEFAULT_EVENTS = ("im.message.receive_v1",)
# "Modify app info". An app that holds it can add scopes to itself through the API
# (PATCH /open-apis/application/v7/applications/<app_id>/config). No app template includes it.
APP_CONFIG_SCOPE = "application:application:patch"

OPEN_HOSTS = {
    "feishu": "https://open.feishu.cn",
    "lark": "https://open.larksuite.com",
}
BRAND_ALIASES = {
    "feishu": "feishu",
    "lark": "lark",
    "larksuite": "lark",
    "international": "lark",
}


def normalize_brand(value: str) -> str:
    """Return ``feishu`` or ``lark``; accept aliases; raise ValueError otherwise."""
    key = (value or "").strip().lower()
    if key not in BRAND_ALIASES:
        raise ValueError(
            f"unknown brand {value!r}; use feishu, lark (aliases: larksuite, international)"
        )
    return BRAND_ALIASES[key]


def open_host(brand: str) -> str:
    return OPEN_HOSTS[normalize_brand(brand)]


def load_scopes(path: Path) -> list[str]:
    scopes: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        scopes.append(line)
    return scopes


def build_url(app_id: str, brand: str, scopes: Sequence[str]) -> str:
    """Classic scope-apply link: a plain page with one confirm button."""
    base = open_host(brand) + "/page/scope-apply"
    encoded = quote(",".join(scopes), safe="")
    return f"{base}?clientID={app_id}&scopes={encoded}"


def build_addons(
    scopes: Iterable[str], events: Iterable[str] = DEFAULT_EVENTS
) -> dict:
    return {
        "scopes": {"tenant": list(scopes), "user": []},
        "events": {"items": {"tenant": list(events), "user": []}},
    }


def encode_addons(addons: dict) -> str:
    """Compact JSON -> gzip (mtime=0, deterministic) -> URL-safe base64, no padding."""
    raw = json.dumps(addons, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    packed = gzip.compress(raw, mtime=0)
    return base64.urlsafe_b64encode(packed).decode("ascii").rstrip("=")


def decode_addons(text: str) -> dict:
    padded = text + "=" * (-len(text) % 4)
    return json.loads(gzip.decompress(base64.urlsafe_b64decode(padded)).decode("utf-8"))


def launcher_url(app_id: str, brand: str, addons: dict) -> str:
    """One-scan update link for an existing app."""
    return (
        f"{open_host(brand)}/page/launcher?clientID={quote(app_id, safe='')}"
        f"&addons={encode_addons(addons)}"
    )


def _brand_arg(value: str) -> str:
    try:
        return normalize_brand(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc))


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
        type=_brand_arg,
        default="feishu",
        help="feishu for China (open.feishu.cn); lark (aliases: larksuite, "
        "international) for international",
    )
    parser.add_argument("--scopes-file", type=Path, default=DEFAULT_SCOPES)
    parser.add_argument(
        "--launcher",
        action="store_true",
        help="print the one-scan launcher link (publishes by itself) instead of "
        "the classic scope-apply link",
    )
    args = parser.parse_args()
    scopes = load_scopes(args.scopes_file)
    if not scopes:
        raise SystemExit(f"no scopes found in {args.scopes_file}")
    if args.launcher:
        print(launcher_url(args.app_id, args.brand, build_addons(scopes)))
    else:
        print(build_url(args.app_id, args.brand, scopes))


if __name__ == "__main__":
    main()
