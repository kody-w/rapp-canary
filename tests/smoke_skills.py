#!/usr/bin/env python3
"""Exercise stored Markdown against a running local candidate, without inference."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid


def exercise(base_url, filesystem=False):
    if urllib.parse.urlsplit(base_url).hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("This smoke test only targets a local candidate.")
    base_url = base_url.rstrip("/")
    identifier = "canary-smoke-" + uuid.uuid4().hex[:12]
    filename = identifier + ".md"
    direct_name = identifier + "-direct"
    direct_filename = direct_name + ".md"
    original = (
        f"\ufeff---\r\nname: {identifier}\r\ndescription: Canary Markdown probe.\r\n"
        "---\r\n\r\nReturn the requested label. Do not generate an agent.\r\n"
    ).encode("utf-8")

    def request(path, method="GET", body=None, headers=None):
        req = urllib.request.Request(base_url + path, data=body, method=method, headers=headers or {})
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.read()

    def read_json(path):
        return json.loads(request(path))

    def upload(content):
        boundary = "brainstem-" + uuid.uuid4().hex
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="SKILL.md"\r\n'
            "Content-Type: text/markdown\r\n\r\n"
        ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
        return json.loads(request(
            "/skills/import", "POST", body, {"Content-Type": "multipart/form-data; boundary=" + boundary},
        ))

    before = {entry["filename"] for entry in read_json("/agents")["files"]}
    created = [filename]
    try:
        result = upload(original)
        assert result["status"] == "ok" and result["scope"] == "skill", result
        assert result["filename"] == filename, result
        assert request("/skills/export/" + filename) == original
        updated = original.replace(b"Canary Markdown probe.", b"Updated Markdown probe.")
        assert upload(updated)["filename"] == filename
        record = next(item for item in read_json("/skills")["files"] if item["filename"] == filename)
        assert record["description"] == "Updated Markdown probe.", record
        assert request("/skills/export/" + filename) == updated

        if filesystem:
            directory = Path(read_json("/skills")["skills_path"])
            direct = directory / direct_filename
            assert not direct.exists(), direct
            created.append(direct_filename)
            direct.write_bytes(
                f"---\nname: {direct_name}\ndescription: Direct drop.\n---\nUse this skill.\n".encode()
            )
            record = next(item for item in read_json("/skills")["files"] if item["filename"] == direct_filename)
            assert record["name"] == direct_name and record["description"] == "Direct drop.", record
            direct.write_bytes(
                f"---\nname: {direct_name}\ndescription: Edited live.\n---\nUse this updated skill.\n".encode()
            )
            record = next(item for item in read_json("/skills")["files"] if item["filename"] == direct_filename)
            assert record["description"] == "Edited live.", record

        after = {entry["filename"] for entry in read_json("/agents")["files"]}
        assert after == before, (before, after)
    finally:
        for created_filename in created:
            try:
                request("/skills/" + created_filename, "DELETE")
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
    remaining = {item["filename"] for item in read_json("/skills")["files"]}
    assert not set(created).intersection(remaining), remaining
    if filesystem:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "rapp_brainstem"))
        from rapp_adapters.skill_frames import ledger_path
        from rapp_adapters.rapp1 import rapp
        ledger = ledger_path(directory)
        frames = list((ledger / "frames").glob("*.json"))
        assert frames, "A zero-frame scan is not RAPP/1 evidence"
        result = subprocess.run(
            [sys.executable, str(Path(rapp.__file__).with_name("rapp_check.py")), str(ledger), "--json"],
            capture_output=True, text=True, check=True,
        )
        verdict = json.loads(result.stdout)
        assert verdict["verdict"] == "COMPLIANT", verdict
        assert any("frames conform" in item["ok"] for item in verdict["evidence"]), verdict
        print(f"PASS RAPP/1: canonical checker scanned {len(frames)} emitted frames: COMPLIANT")
    print("PASS Markdown upload, byte-exact export, persistent listing, live edits, deletion, and no generated agents")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:7071")
    parser.add_argument("--filesystem", action="store_true", help="Also drop and edit a file on this same machine.")
    args = parser.parse_args()
    exercise(args.url, args.filesystem)
