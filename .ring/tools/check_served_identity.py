#!/usr/bin/env python3
"""Deployment gate: prove the SERVED artifacts point at this ring, not at grail.

Two modes, same checks:

  --site DIR   the assembled Pages site before deploy (pre-deploy gate)
  --url  BASE  the live Pages origin after deploy (post-deploy probe)

Checks, per entry point a human or an AI is told to open:

  1. no rewrite source (grail identity) survives anywhere in the file;
  2. identity files name this ring (repository slug or Pages host);
  3. every skill.md link on the landing page is a Pages URL of THIS ring,
     never a raw.githubusercontent.com blob (raw blobs carry grail identity by
     design — see .ring/SERVE-TIME-IDENTITY.md).

Exit 0 only when every check passes. A failure names the file and the reason.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# What a reader is told to fetch. index.html is the landing page (served at /).
ENTRY_POINTS = (
    "install.sh",
    "install.ps1",
    "install.cmd",
    "install.command",
    "skill.md",
    "skills/rapp-bootstrap/SKILL.md",
    "index.html",
)
# Files that must name the ring's repository outright.
IDENTITY_REQUIRED = (
    "install.sh",
    "install.ps1",
    "skill.md",
    "skills/rapp-bootstrap/SKILL.md",
    "index.html",
)
_SKILL_LINK = re.compile(r"https?://[^\s\"'<>)]*skill\.md")


class GateError(RuntimeError):
    pass


def _config(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GateError(f"invalid ring config: {error}") from error
    for key in ("repository", "pages_url", "rewrites"):
        if key not in value:
            raise GateError(f"ring config missing {key!r}")
    return value


def _read_site(site: Path, relative: str) -> str | None:
    path = site / relative
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def _read_live(base: str, relative: str, attempts: int, wait: float) -> str | None:
    url = base.rstrip("/") + "/" + ("" if relative == "index.html" else relative)
    last = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(
                url, headers={"Cache-Control": "no-cache", "User-Agent": "rapp-ring-gate"}
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            if error.code == 404:
                # Not served (yet). Keep retrying for propagation; after the last
                # attempt report it as missing so every other failure is listed too.
                last = f"404 {url}"
                if attempt + 1 == attempts:
                    return None
            else:
                last = f"{error.code} {url}"
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last = f"{error} {url}"
        if attempt + 1 < attempts:
            time.sleep(wait)
    raise GateError(f"could not fetch entry point after {attempts} attempts: {last}")


def check(config: dict, reader) -> dict:
    """Run every check. `reader(relative) -> text | None`."""
    repository = config["repository"]
    pages_url = config["pages_url"].rstrip("/")
    # A file names the ring either by repository slug or by its Pages host+path.
    identities = (repository, pages_url.split("://", 1)[-1])
    needles = [rule["from"] for rule in config["rewrites"] if rule.get("from")]
    failures: list[str] = []
    checked: list[str] = []
    for relative in ENTRY_POINTS:
        text = reader(relative)
        if text is None:
            failures.append(f"{relative}: missing from served site")
            continue
        checked.append(relative)
        for needle in needles:
            if needle in text:
                failures.append(
                    f"{relative}: grail identity survived rendering: {needle!r}"
                )
        if relative in IDENTITY_REQUIRED and not any(i in text for i in identities):
            failures.append(
                f"{relative}: does not name this ring ({' or '.join(identities)})"
            )
        if relative == "index.html":
            for link in _SKILL_LINK.findall(text):
                if "raw.githubusercontent.com" in link:
                    failures.append(
                        f"{relative}: skill link is a raw blob (grail bytes): {link}"
                    )
                elif not link.startswith(pages_url + "/"):
                    failures.append(
                        f"{relative}: skill link leaves this ring's Pages origin: {link}"
                    )
    return {
        "schema": "rapp-ring-gate/1",
        "ring": config.get("name"),
        "repository": repository,
        "pages_url": pages_url,
        "checked": checked,
        "failures": failures,
        "ok": not failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--site", type=Path, help="assembled site directory")
    target.add_argument("--url", help="live Pages origin, e.g. https://kody-w.github.io/rapp-canary")
    parser.add_argument("--attempts", type=int, default=12,
                        help="live mode: fetch attempts per file (CDN propagation)")
    parser.add_argument("--wait", type=float, default=10.0,
                        help="live mode: seconds between attempts")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        config = _config(args.config.resolve())
        if args.site is not None:
            site = args.site.resolve()
            if not site.is_dir():
                raise GateError(f"site directory missing: {site}")
            result = check(config, lambda rel: _read_site(site, rel))
            result["target"] = str(site)
        else:
            base = args.url
            result = check(
                config,
                lambda rel: _read_live(base, rel, args.attempts, args.wait),
            )
            result["target"] = base
    except GateError as error:
        print(f"served-identity gate failed: {error}", file=sys.stderr)
        return 1
    if args.report:
        args.report.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    for failure in result["failures"]:
        print(f"✗ {failure}", file=sys.stderr)
    if result["ok"]:
        print(
            f"✓ served identity is {result['repository']} "
            f"({len(result['checked'])} entry points checked at {result['target']})"
        )
        return 0
    print(
        f"✗ served identity gate: {len(result['failures'])} failure(s) at {result['target']}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
