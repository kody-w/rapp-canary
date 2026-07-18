import json
import os
import subprocess

from agents.basic_agent import BasicAgent


class TrainStatusAgent(BasicAgent):
    """Live train picture for the twin's home repo (TWIN_REPO_ROOT)."""

    def __init__(self):
        self.name = "TrainStatus"
        self.metadata = {
            "name": self.name,
            "description": (
                "Report the live state of this repo's release-train ring: ring "
                "identity, payload version, HEAD commit, and train topology. "
                "Use whenever asked about the train, the ring, or versions."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        }
        super().__init__(name=self.name, metadata=self.metadata)

    def perform(self, **kwargs):
        repo = os.environ.get("TWIN_REPO_ROOT", "")
        if not repo or not os.path.isdir(repo):
            return "TWIN_REPO_ROOT is not set — the twin was not launched via twin.sh."
        lines = []

        ring_path = os.path.join(repo, ".ring", "ring.json")
        if os.path.exists(ring_path):
            try:
                ring = json.load(open(ring_path))
                lines.append(f"ring: {ring.get('ring', 'unknown')} ({ring.get('repository', '?')})")
            except Exception as e:
                lines.append(f"ring.json unreadable: {e}")

        ver_path = os.path.join(repo, "rapp_brainstem", "VERSION")
        if os.path.exists(ver_path):
            lines.append(f"payload version: {open(ver_path).read().strip()}")

        try:
            head = subprocess.run(
                ["git", "-C", repo, "log", "-1", "--format=%h %ad %s", "--date=short"],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip()
            branch = subprocess.run(
                ["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip()
            lines.append(f"HEAD: {head} (branch {branch})")
        except Exception as e:
            lines.append(f"git unavailable: {e}")

        train_path = os.path.join(repo, ".ring", "train.json")
        if os.path.exists(train_path):
            try:
                train = json.load(open(train_path))
                rings = " -> ".join(r["name"] for r in train.get("rings", []))
                lines.append(f"train: {rings} (grail is human-merge only)")
            except Exception as e:
                lines.append(f"train.json unreadable: {e}")

        return "\n".join(lines) if lines else "no train metadata found in this repo"
