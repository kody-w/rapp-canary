"""Tests for deterministic ring-specific URL rendering."""

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / ".ring" / "tools" / "render_ring.py"
SPEC = importlib.util.spec_from_file_location("render_ring", MODULE_PATH)
RENDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RENDER)


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


class RenderRingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "source"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.name", "Render Test")
        _git(self.repo, "config", "user.email", "render@example.invalid")
        (self.repo / "install.txt").write_text(
            "repo=kody-w/rapp-installer\n"
            "pages=kody-w.github.io/rapp-installer\n",
            encoding="utf-8",
            newline="\n",
        )
        (self.repo / ".ring").mkdir()
        _git(self.repo, "add", "install.txt")
        _git(self.repo, "commit", "-qm", "payload")

    def tearDown(self):
        self.temp.cleanup()

    def _config(self, ring, repo, pages, expected_repo=1):
        path = self.root / f"{ring}.json"
        path.write_text(
            json.dumps({
                "schema": "rapp-ring/1",
                "name": ring,
                "repository": repo,
                "pages_url": f"https://{pages}",
                "support_repository": repo,
                "parent": None if ring == "canary" else "canary",
                "rewrites": [
                    {
                        "from": "kody-w.github.io/rapp-installer",
                        "to": pages,
                        "expected_count": 1,
                    },
                    {
                        "from": "kody-w/rapp-installer",
                        "to": repo,
                        "expected_count": expected_repo,
                    },
                ],
                "protected_paths": [".ring/"],
            }),
            encoding="utf-8",
            newline="\n",
        )
        return path

    def test_same_payload_renders_different_ring_urls(self):
        canary_dir = self.root / "canary-build"
        nightly_dir = self.root / "nightly-build"
        canary = RENDER.render(
            self.repo,
            self._config(
                "canary",
                "kody-w/rapp-canary",
                "kody-w.github.io/rapp-canary",
            ),
            canary_dir,
        )
        nightly = RENDER.render(
            self.repo,
            self._config(
                "nightly",
                "kody-w/rapp-nightly",
                "kody-w.github.io/rapp-nightly",
            ),
            nightly_dir,
        )

        self.assertNotEqual(
            canary["rendered_sha256"],
            nightly["rendered_sha256"],
        )
        self.assertIn(
            "kody-w/rapp-canary",
            (canary_dir / "install.txt").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "kody-w/rapp-nightly",
            (nightly_dir / "install.txt").read_text(encoding="utf-8"),
        )

    def test_rewrite_count_drift_fails(self):
        with self.assertRaisesRegex(RENDER.RenderError, "rewrite count drift"):
            RENDER.render(
                self.repo,
                self._config(
                    "canary",
                    "kody-w/rapp-canary",
                    "kody-w.github.io/rapp-canary",
                    expected_repo=2,
                ),
                self.root / "broken-build",
            )


if __name__ == "__main__":
    unittest.main()
