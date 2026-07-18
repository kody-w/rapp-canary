"""Tests for deterministic Canary -> Nightly payload attestations."""

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / ".ring" / "tools" / "ring_attestation.py"
PROMOTE_PATH = REPO_ROOT / ".ring" / "tools" / "promote_ring.py"
CONFIG_PATH = REPO_ROOT / ".ring" / "train.json"
if str(MODULE_PATH.parent) not in sys.path:
    sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("ring_attestation", MODULE_PATH)
RING = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RING)
PROMOTE_SPEC = importlib.util.spec_from_file_location(
    "promote_ring",
    PROMOTE_PATH,
)
PROMOTE = importlib.util.module_from_spec(PROMOTE_SPEC)
PROMOTE_SPEC.loader.exec_module(PROMOTE)


def _git(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class RingAttestationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "payload"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.name", "Ring Test")
        _git(self.repo, "config", "user.email", "ring@example.invalid")
        _git(
            self.repo,
            "remote",
            "add",
            "origin",
            "https://github.com/kody-w/rapp-canary.git",
        )
        (self.repo / "payload.txt").write_text(
            "immutable payload\n",
            encoding="utf-8",
            newline="\n",
        )
        (self.repo / ".ring").mkdir()
        (self.repo / ".ring/ring.json").write_text(
            '{"ring":"canary","url":"https://kody-w.github.io/rapp-canary"}\n',
            encoding="utf-8",
            newline="\n",
        )
        (self.repo / "run.sh").write_text(
            "#!/bin/sh\nexit 0\n",
            encoding="utf-8",
            newline="\n",
        )
        required = (
            self.repo
            / "rapp_brainstem"
            / "agents"
            / "experimental"
            / "copilot_research_agent.py"
        )
        required.parent.mkdir(parents=True)
        required.write_text(
            "# required compatibility agent\n",
            encoding="utf-8",
            newline="\n",
        )
        run_mode = (self.repo / "run.sh").stat().st_mode
        os.chmod(
            self.repo / "run.sh",
            run_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH,
        )
        _git(
            self.repo,
            "add",
            "payload.txt",
            "run.sh",
            "rapp_brainstem/agents/experimental/copilot_research_agent.py",
            ".ring/ring.json",
        )
        _git(self.repo, "update-index", "--chmod=+x", "run.sh")
        _git(self.repo, "commit", "-qm", "candidate")
        self.commit = _git(self.repo, "rev-parse", "HEAD^{commit}")
        self.nightly_repo = self.root / "nightly-payload"
        result = subprocess.run(
            ["git", "clone", "-q", str(self.repo), str(self.nightly_repo)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise AssertionError(result.stderr)
        _git(
            self.nightly_repo,
            "remote",
            "set-url",
            "origin",
            "https://github.com/kody-w/rapp-nightly.git",
        )
        _git(self.nightly_repo, "config", "user.name", "Nightly Test")
        _git(
            self.nightly_repo,
            "config",
            "user.email",
            "nightly@example.invalid",
        )
        (self.nightly_repo / ".ring/ring.json").write_text(
            '{"ring":"nightly","url":"https://kody-w.github.io/rapp-nightly"}\n',
            encoding="utf-8",
            newline="\n",
        )
        _git(self.nightly_repo, "add", ".ring/ring.json")
        _git(self.nightly_repo, "commit", "-qm", "nightly overlay")
        self.nightly_commit = _git(
            self.nightly_repo,
            "rev-parse",
            "HEAD^{commit}",
        )
        self.extra_rings = {}
        for name, parent in (("alpha", "nightly"), ("beta", "alpha")):
            path = self.root / f"{name}-payload"
            result = subprocess.run(
                ["git", "clone", "-q", str(self.repo), str(path)],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode:
                raise AssertionError(result.stderr)
            _git(
                path,
                "remote",
                "set-url",
                "origin",
                f"https://github.com/kody-w/rapp-{name}.git",
            )
            _git(path, "config", "user.name", f"{name.title()} Test")
            _git(
                path,
                "config",
                "user.email",
                f"{name}@example.invalid",
            )
            (path / ".ring/ring.json").write_text(
                json.dumps({
                    "ring": name,
                    "parent": parent,
                    "url": f"https://kody-w.github.io/rapp-{name}",
                }) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            _git(path, "add", ".ring/ring.json")
            _git(path, "commit", "-qm", f"{name} overlay")
            self.extra_rings[name] = (
                path,
                _git(path, "rev-parse", "HEAD^{commit}"),
            )
        self.canary = self.root / "canary.json"
        self.nightly = self.root / "nightly.json"

    def tearDown(self):
        self.temp.cleanup()

    def _create_canary(self):
        return RING.create_attestation(
            "canary",
            self.repo,
            "kody-w/rapp-canary",
            self.commit,
            CONFIG_PATH,
            self.canary,
            None,
        )

    def test_same_payload_promotes_canary_to_nightly(self):
        canary = self._create_canary()
        nightly = RING.create_attestation(
            "nightly",
            self.nightly_repo,
            "kody-w/rapp-nightly",
            self.nightly_commit,
            CONFIG_PATH,
            self.nightly,
            self.canary,
        )

        self.assertNotEqual(
            canary["payload"]["repository"],
            nightly["payload"]["repository"],
        )
        self.assertEqual(
            canary["payload"]["shared_sha256"],
            nightly["payload"]["shared_sha256"],
        )
        self.assertNotEqual(
            canary["payload"]["tree"],
            nightly["payload"]["tree"],
        )
        self.assertEqual(nightly["parent"]["ring"], "canary")
        verified = RING.verify_attestation(
            self.nightly,
            self.nightly_repo,
            "kody-w/rapp-nightly",
            self.nightly_commit,
            CONFIG_PATH,
            self.canary,
        )
        self.assertEqual(verified, nightly)

    def test_rebuild_cannot_cross_ring_boundary(self):
        self._create_canary()
        (self.nightly_repo / "payload.txt").write_text(
            "different build\n",
            encoding="utf-8",
            newline="\n",
        )
        _git(self.nightly_repo, "add", "payload.txt")
        _git(self.nightly_repo, "commit", "-qm", "rebuilt")
        rebuilt_commit = _git(
            self.nightly_repo,
            "rev-parse",
            "HEAD^{commit}",
        )

        with self.assertRaisesRegex(
            RING.AttestationError,
            "payload changed between rings",
        ):
            RING.create_attestation(
                "nightly",
                self.nightly_repo,
                "kody-w/rapp-nightly",
                rebuilt_commit,
                CONFIG_PATH,
                self.nightly,
                self.canary,
            )

    def test_nightly_requires_canary_parent(self):
        with self.assertRaisesRegex(
            RING.AttestationError,
            "requires a canary parent",
        ):
            RING.create_attestation(
                "nightly",
                self.nightly_repo,
                "kody-w/rapp-nightly",
                self.nightly_commit,
                CONFIG_PATH,
                self.nightly,
                None,
            )

    def test_attestation_repository_is_bound_to_ring_config(self):
        with self.assertRaisesRegex(
            RING.AttestationError,
            "repository must be kody-w/rapp-canary",
        ):
            RING.create_attestation(
                "canary",
                self.repo,
                "kody-w/rapp-nightly",
                self.commit,
                CONFIG_PATH,
                self.canary,
                None,
            )

    def test_verification_rejects_child_with_different_shared_payload(self):
        parent = self._create_canary()
        (self.nightly_repo / "payload.txt").write_text(
            "different nightly payload\n",
            encoding="utf-8",
            newline="\n",
        )
        _git(self.nightly_repo, "add", "payload.txt")
        _git(self.nightly_repo, "commit", "-qm", "different nightly payload")
        commit = _git(self.nightly_repo, "rev-parse", "HEAD^{commit}")
        config = RING._read_json(CONFIG_PATH)
        payload = RING._payload(
            self.nightly_repo,
            "kody-w/rapp-nightly",
            commit,
            RING._ring_owned_prefixes(config),
        )
        forged = {
            "schema": "rapp-ring-attestation/1",
            "ring": "nightly",
            "payload": payload,
            "parent": {
                "ring": "canary",
                "sha256": RING._attestation_sha256(parent),
            },
            "result": "passed",
        }
        self.nightly.write_text(
            json.dumps(forged, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )

        with self.assertRaisesRegex(
            RING.AttestationError,
            "different shared payloads",
        ):
            RING.verify_attestation(
                self.nightly,
                self.nightly_repo,
                "kody-w/rapp-nightly",
                commit,
                CONFIG_PATH,
                self.canary,
            )

    def test_attestation_is_deterministic(self):
        first = self._create_canary()
        first_bytes = self.canary.read_bytes()
        second = self._create_canary()

        self.assertEqual(first, second)
        self.assertEqual(first_bytes, self.canary.read_bytes())
        parsed = json.loads(first_bytes)
        self.assertNotIn("generated_at", parsed)

    def test_shape_promotion_preserves_nightly_overlay(self):
        nightly_overlay = (
            self.nightly_repo / ".ring/ring.json"
        ).read_bytes()
        (self.repo / "payload.txt").write_text(
            "new shared Canary build\n",
            encoding="utf-8",
            newline="\n",
        )
        _git(self.repo, "add", "payload.txt")
        _git(self.repo, "commit", "-qm", "canary shared change")
        source_commit = _git(self.repo, "rev-parse", "HEAD^{commit}")

        lock = PROMOTE.promote(
            self.repo,
            self.nightly_repo,
            "canary",
            "nightly",
            source_commit,
            self.nightly_commit,
            CONFIG_PATH,
        )

        self.assertEqual(
            (self.nightly_repo / "payload.txt").read_text(encoding="utf-8"),
            "new shared Canary build\n",
        )
        self.assertEqual(
            (self.nightly_repo / ".ring/ring.json").read_bytes(),
            nightly_overlay,
        )
        self.assertEqual(lock["source"]["ring"], "canary")
        self.assertEqual(lock["target"]["ring"], "nightly")
        self.assertTrue(
            _git(
                self.nightly_repo,
                "ls-files",
                "-s",
                "run.sh",
            ).startswith("100755 ")
        )

        _git(self.nightly_repo, "commit", "-qm", "promote Canary payload")
        nightly_commit = _git(
            self.nightly_repo,
            "rev-parse",
            "HEAD^{commit}",
        )
        canary = RING.create_attestation(
            "canary",
            self.repo,
            "kody-w/rapp-canary",
            source_commit,
            CONFIG_PATH,
            self.canary,
            None,
        )
        nightly = RING.create_attestation(
            "nightly",
            self.nightly_repo,
            "kody-w/rapp-nightly",
            nightly_commit,
            CONFIG_PATH,
            self.nightly,
            self.canary,
        )
        self.assertEqual(
            canary["payload"]["shared_sha256"],
            nightly["payload"]["shared_sha256"],
        )

    def test_full_pre_grail_attestation_chain(self):
        canary = self._create_canary()
        previous = canary
        previous_path = self.canary
        for name, repository in (
            ("nightly", "kody-w/rapp-nightly"),
            ("alpha", "kody-w/rapp-alpha"),
            ("beta", "kody-w/rapp-beta"),
        ):
            if name == "nightly":
                repo = self.nightly_repo
                commit = self.nightly_commit
            else:
                repo, commit = self.extra_rings[name]
            output = self.root / f"{name}.json"
            current = RING.create_attestation(
                name,
                repo,
                repository,
                commit,
                CONFIG_PATH,
                output,
                previous_path,
            )
            self.assertEqual(
                current["payload"]["shared_sha256"],
                canary["payload"]["shared_sha256"],
            )
            previous = current
            previous_path = output
        self.assertEqual(previous["ring"], "beta")

    def test_automated_promotion_stops_before_grail(self):
        beta_repo, beta_commit = self.extra_rings["beta"]
        grail = self.root / "grail"
        result = subprocess.run(
            ["git", "clone", "-q", str(self.repo), str(grail)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise AssertionError(result.stderr)
        _git(
            grail,
            "remote",
            "set-url",
            "origin",
            "https://github.com/kody-w/rapp-installer.git",
        )
        grail_commit = _git(grail, "rev-parse", "HEAD^{commit}")

        with self.assertRaisesRegex(
            PROMOTE.PromotionError,
            "human-controlled promotion",
        ):
            PROMOTE.promote(
                beta_repo,
                grail,
                "beta",
                "grail",
                beta_commit,
                grail_commit,
                CONFIG_PATH,
            )

    def test_required_shared_file_deletion_is_rejected(self):
        required = (
            self.repo
            / "rapp_brainstem"
            / "agents"
            / "experimental"
            / "copilot_research_agent.py"
        )
        required.unlink()
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "delete required agent")
        source_commit = _git(self.repo, "rev-parse", "HEAD^{commit}")

        with self.assertRaisesRegex(
            PROMOTE.PromotionError,
            "required shared paths are missing",
        ):
            PROMOTE.promote(
                self.repo,
                self.nightly_repo,
                "canary",
                "nightly",
                source_commit,
                self.nightly_commit,
                CONFIG_PATH,
            )

    def test_directory_to_file_transition_is_preflighted(self):
        (self.repo / "shape").write_text(
            "now a file\n",
            encoding="utf-8",
            newline="\n",
        )
        _git(self.repo, "add", "shape")
        _git(self.repo, "commit", "-qm", "source file shape")
        source_commit = _git(self.repo, "rev-parse", "HEAD^{commit}")

        (self.nightly_repo / "shape").mkdir()
        (self.nightly_repo / "shape/child.txt").write_text(
            "old child\n",
            encoding="utf-8",
            newline="\n",
        )
        _git(self.nightly_repo, "add", "shape/child.txt")
        _git(self.nightly_repo, "commit", "-qm", "target directory shape")
        target_commit = _git(
            self.nightly_repo,
            "rev-parse",
            "HEAD^{commit}",
        )

        PROMOTE.promote(
            self.repo,
            self.nightly_repo,
            "canary",
            "nightly",
            source_commit,
            target_commit,
            CONFIG_PATH,
        )

        self.assertTrue((self.nightly_repo / "shape").is_file())
        self.assertEqual(
            (self.nightly_repo / "shape").read_text(encoding="utf-8"),
            "now a file\n",
        )

    def test_promotion_lock_symlink_is_rejected(self):
        outside = self.root / "outside.txt"
        outside.write_text("do not overwrite\n", encoding="utf-8")
        link = self.nightly_repo / ".ring/upstream.lock.json"
        try:
            os.symlink(outside, link)
        except OSError:
            self.skipTest("symlink creation is unavailable")
        _git(self.nightly_repo, "add", ".ring/upstream.lock.json")
        _git(self.nightly_repo, "commit", "-qm", "malicious lock symlink")
        target_commit = _git(
            self.nightly_repo,
            "rev-parse",
            "HEAD^{commit}",
        )

        with self.assertRaisesRegex(
            PROMOTE.PromotionError,
            "lock must not be a symlink",
        ):
            PROMOTE.promote(
                self.repo,
                self.nightly_repo,
                "canary",
                "nightly",
                self.commit,
                target_commit,
                CONFIG_PATH,
            )
        self.assertEqual(
            outside.read_text(encoding="utf-8"),
            "do not overwrite\n",
        )


if __name__ == "__main__":
    unittest.main()
