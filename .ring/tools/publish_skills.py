#!/usr/bin/env python3
"""Publish rendered onboarding skills with ring-correct public fallback URLs."""

import argparse
import json
from pathlib import Path


def publish(rendered, site, config):
    identity = json.loads(config.read_text(encoding="utf-8"))
    repository = identity["repository"]
    pages = identity["pages_url"].rstrip("/")
    raw = f"https://raw.githubusercontent.com/{repository}/main"
    for relative in ("skill.md", "skills/rapp-bootstrap/SKILL.md"):
        source = rendered / relative
        text = source.read_text(encoding="utf-8")
        # Raw ring payloads retain Grail identity. Public installer/playbook
        # fallbacks must use the ring-rendered Pages copies instead.
        text = text.replace(raw + "/install.ps1", pages + "/install.ps1")
        text = text.replace(raw + "/skill.md", pages + "/skill.md")
        destination = site / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8", newline="\n")
    return [pages + "/skill.md", pages + "/skills/rapp-bootstrap/SKILL.md"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rendered", type=Path, required=True)
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(publish(args.rendered, args.site, args.config)))
