import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("publish_skills", ROOT / ".ring/tools/publish_skills.py")
publication = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publication)


class PublishedSkillsTests(unittest.TestCase):
    def test_publication_includes_canonical_and_router_with_safe_ring_urls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rendered = root / "rendered"
            (rendered / "skills/rapp-bootstrap").mkdir(parents=True)
            canonical = "https://raw.githubusercontent.com/kody-w/rapp-canary/main/install.ps1"
            router = "https://raw.githubusercontent.com/kody-w/rapp-canary/main/skill.md"
            (rendered / "skill.md").write_text(canonical, encoding="utf-8")
            (rendered / "skills/rapp-bootstrap/SKILL.md").write_text(router, encoding="utf-8")
            config = root / "ring.json"
            config.write_text(json.dumps({
                "repository": "kody-w/rapp-canary",
                "pages_url": "https://kody-w.github.io/rapp-canary",
            }), encoding="utf-8")
            publication.publish(rendered, root / "site", config)
            self.assertEqual(
                (root / "site/skill.md").read_text(),
                "https://kody-w.github.io/rapp-canary/install.ps1",
            )
            self.assertEqual(
                (root / "site/skills/rapp-bootstrap/SKILL.md").read_text(),
                "https://kody-w.github.io/rapp-canary/skill.md",
            )
            self.assertEqual((rendered / "skill.md").read_text(), canonical)

    def test_setup_only_refuses_to_replace_an_existing_flight(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "src").mkdir()
            marker = home / "src/user-data"
            marker.write_text("preserve", encoding="utf-8")
            env = dict(os.environ, FLIGHT_HOME=str(home), FLIGHT_SETUP_ONLY="1")
            result = subprocess.run(
                ["bash", str(ROOT / ".ring/pages/flight.sh"), "canary"],
                env=env, capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Existing flight preserved", result.stderr)
            self.assertEqual(marker.read_text(), "preserve")
