"""Real emitted receipts must pass the pinned canonical RAPP/1 checker."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

import brainstem as bs
from rapp_adapters import skill_frames
from rapp_adapters import skills as adapter
from rapp_adapters.rapp1 import rapp as R
from test_skill_import import SKILL, client, generation, upload


def test_canonical_reference_files_are_unmodified():
    directory = Path(R.__file__).parent
    pin = json.loads((directory / "PROVENANCE.json").read_text(encoding="utf-8"))
    assert pin["commit"] == "eb50008011447f5e69372ac22a1755f0978d15ed"
    for filename, digest in pin["files"].items():
        assert hashlib.sha256((directory / filename).read_bytes()).hexdigest() == digest


def test_real_skill_receipts_pass_the_canonical_checker(client):
    stored = upload(client, mode=None).json
    used = json.loads(adapter.learner(bs).perform(action="use", name="workshop-helper"))
    converted = upload(client, mode="agent").json
    deleted = client.delete("/skills/workshop-helper.md").json
    receipts = [item["frame"] for item in (stored, used, converted, deleted)]
    assert [frame["kind"] for frame in receipts] == [
        "skill.store", "skill.use", "skill.convert", "skill.delete",
    ]
    head = None
    for frame in receipts:
        assert set(frame) == R.FRAME_KEYS
        assert R.verify_frame(frame, head=head, stream_id_of_record=receipts[0]["stream_id"])[0]
        head = frame
    assert receipts[0]["payload"]["source_sha256"] == hashlib.sha256(SKILL.encode()).hexdigest()
    root = skill_frames.ledger_path(bs.SKILLS_PATH)
    assert len(list((root / "frames").glob("*.json"))) == 4
    result = subprocess.run(
        [sys.executable, str(Path(R.__file__).with_name("rapp_check.py")), str(root), "--json"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["verdict"] == "COMPLIANT", report
    assert any("4 frames conform" in item["ok"] for item in report["evidence"]), report


def test_corrupt_receipts_are_refused_not_repaired(client):
    upload(client, mode=None)
    path = next((skill_frames.ledger_path(bs.SKILLS_PATH) / "frames").glob("*.json"))
    corrupted = json.loads(path.read_text(encoding="utf-8"))
    corrupted["payload"]["bytes"] += 1
    path.write_text(json.dumps(corrupted), encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(ValueError, match="ledger refused"):
        skill_frames.emit(bs.SKILLS_PATH, "use", {"name": "workshop-helper"})
    assert path.read_bytes() == original
    assert len(list(path.parent.glob("*.json"))) == 1
