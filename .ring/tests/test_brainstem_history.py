import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / ".ring" / "tools" / "brainstem_history.py"
SPEC = importlib.util.spec_from_file_location("brainstem_history", MODULE)
HISTORY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HISTORY)


def git(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class BrainstemHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "grail"
        (self.repo / "rapp_brainstem").mkdir(parents=True)
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.name", "History Test")
        git(self.repo, "config", "user.email", "history@example.invalid")
        git(
            self.repo,
            "remote",
            "add",
            "origin",
            "https://github.com/kody-w/rapp-installer.git",
        )

    def tearDown(self):
        self.temp.cleanup()

    def release(self, version, body):
        (self.repo / "rapp_brainstem" / "VERSION").write_text(
            version + "\n", encoding="utf-8"
        )
        (self.repo / "rapp_brainstem" / "brainstem.py").write_text(
            body, encoding="utf-8"
        )
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", f"release {version}")
        tag = f"brainstem-v{version}"
        git(self.repo, "tag", tag)
        return tag

    def test_frames_form_a_deterministic_known_good_chain(self):
        first_tag = self.release("1.0.0", "print('stable one')\n")
        first_path = self.root / f"{first_tag}.json"
        first = HISTORY.create_frame(self.repo, first_tag, first_path)
        second_tag = self.release("1.1.0", "print('stable two')\n")
        second_path = self.root / f"{second_tag}.json"
        second = HISTORY.create_frame(
            self.repo, second_tag, second_path, first_path
        )

        self.assertEqual(
            second["parent"]["sha256"],
            HISTORY.frame_sha256(first),
        )
        self.assertEqual(
            HISTORY.verify_frame(self.repo, second_path, first_path),
            second,
        )
        self.assertEqual(HISTORY.verify_frame(self.repo, second_path), second)
        count, tip = HISTORY.verify_history(self.repo, self.root)
        self.assertEqual(count, 2)
        self.assertEqual(tip, second)

    def test_missing_release_frame_breaks_the_history(self):
        first_tag = self.release("1.0.0", "print('stable one')\n")
        HISTORY.create_frame(
            self.repo,
            first_tag,
            self.root / f"{first_tag}.json",
        )
        self.release("1.1.0", "print('stable two')\n")
        with self.assertRaisesRegex(HISTORY.HistoryError, "missing"):
            HISTORY.verify_history(self.repo, self.root)

    def test_moved_release_tag_is_rejected(self):
        tag = self.release("1.0.0", "print('stable')\n")
        frame_path = self.root / "frame.json"
        HISTORY.create_frame(self.repo, tag, frame_path)
        (self.repo / "rapp_brainstem" / "brainstem.py").write_text(
            "print('different')\n", encoding="utf-8"
        )
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "different")
        git(self.repo, "tag", "-f", tag)
        with self.assertRaisesRegex(HISTORY.HistoryError, "moved"):
            HISTORY.verify_frame(self.repo, frame_path)

    def test_tampered_frame_is_rejected(self):
        tag = self.release("1.0.0", "print('stable')\n")
        frame_path = self.root / "frame.json"
        HISTORY.create_frame(self.repo, tag, frame_path)
        value = json.loads(frame_path.read_text(encoding="utf-8"))
        value["brainstem"]["sha256"] = "0" * 64
        frame_path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(HISTORY.HistoryError, "does not match"):
            HISTORY.verify_frame(self.repo, frame_path)


if __name__ == "__main__":
    unittest.main()
