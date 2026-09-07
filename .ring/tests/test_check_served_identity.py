"""Tests for the served-identity deployment gate."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / ".ring" / "tools" / "check_served_identity.py"
SPEC = importlib.util.spec_from_file_location("check_served_identity", MODULE_PATH)
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)

CONFIG = {
    "schema": "rapp-ring/1",
    "name": "canary",
    "repository": "kody-w/rapp-canary",
    "pages_url": "https://kody-w.github.io/rapp-canary",
    "rewrites": [
        {"from": "kody-w.github.io/rapp-installer",
         "to": "kody-w.github.io/rapp-canary", "expected_count": 1},
        {"from": "kody-w/rapp-installer",
         "to": "kody-w/rapp-canary", "expected_count": 1},
    ],
}


def _good_site(root: Path):
    ring = "kody-w/rapp-canary"
    pages = "https://kody-w.github.io/rapp-canary"
    files = {
        "install.sh": f'REPO_URL="https://github.com/{ring}.git"\n',
        "install.ps1": f'$RepoUrl = "https://github.com/{ring}.git"\n',
        "install.cmd": "powershell -c irm ... | iex\n",
        "install.command": f"curl -fsSL {pages}/install.sh | bash\n",
        "skill.md": f"---\nname: rapp-brainstem\nmetadata: {{\"repo\":\"https://github.com/{ring}\"}}\n---\n\n# RAPP Brainstem\n",
        "skills/rapp-bootstrap/SKILL.md": f"Read {pages}/skill.md for {ring}.\n",
        "index.html": (
            f'<a href="{pages}/skill.md">Read the guide</a>\n'
            f'<textarea>Read {pages}/skill.md. Help me set up {ring}.</textarea>\n'
        ),
    }
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")


class ServedIdentityGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.site = Path(self.temp.name)
        _good_site(self.site)

    def tearDown(self):
        self.temp.cleanup()

    def _run(self):
        return GATE.check(CONFIG, lambda rel: GATE._read_site(self.site, rel))

    def test_ring_identity_site_passes(self):
        result = self._run()
        self.assertTrue(result["ok"], result["failures"])
        self.assertEqual(len(result["checked"]), len(GATE.ENTRY_POINTS))

    def test_raw_skill_link_on_landing_page_fails(self):
        (self.site / "index.html").write_text(
            '<a href="https://raw.githubusercontent.com/kody-w/rapp-canary/main/skill.md">guide</a>\n'
            "kody-w/rapp-canary\n",
            encoding="utf-8", newline="\n",
        )
        result = self._run()
        self.assertFalse(result["ok"])
        self.assertTrue(any("raw blob" in f for f in result["failures"]), result["failures"])

    def test_surviving_grail_identity_fails(self):
        (self.site / "skill.md").write_text(
            "---\nname: x\n---\nhomepage: https://kody-w.github.io/rapp-installer/\nkody-w/rapp-canary\n",
            encoding="utf-8", newline="\n",
        )
        result = self._run()
        self.assertFalse(result["ok"])
        self.assertTrue(any("grail identity survived" in f for f in result["failures"]))

    def test_missing_entry_point_fails(self):
        (self.site / "skill.md").unlink()
        result = self._run()
        self.assertFalse(result["ok"])
        self.assertIn("skill.md: missing from served site", result["failures"])

    def test_skill_link_to_other_ring_fails(self):
        (self.site / "index.html").write_text(
            '<a href="https://kody-w.github.io/rapp-nightly/skill.md">guide</a> kody-w/rapp-canary\n',
            encoding="utf-8", newline="\n",
        )
        result = self._run()
        self.assertFalse(result["ok"])
        self.assertTrue(any("leaves this ring" in f for f in result["failures"]))

    def test_identity_file_without_ring_name_fails(self):
        (self.site / "install.sh").write_text("echo hello\n", encoding="utf-8", newline="\n")
        result = self._run()
        self.assertFalse(result["ok"])
        self.assertIn(
            "install.sh: does not name this ring (kody-w/rapp-canary or kody-w.github.io/rapp-canary)",
            result["failures"],
        )

    def test_pages_host_alone_counts_as_ring_identity(self):
        (self.site / "skills/rapp-bootstrap/SKILL.md").write_text(
            "Read https://kody-w.github.io/rapp-canary/skill.md\n",
            encoding="utf-8", newline="\n",
        )
        result = self._run()
        self.assertTrue(result["ok"], result["failures"])


if __name__ == "__main__":
    unittest.main()
