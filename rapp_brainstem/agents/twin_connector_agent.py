import glob
import json
import os
import subprocess
import urllib.request

from agents.basic_agent import BasicAgent


class TwinConnectorAgent(BasicAgent):
    """Drive any repo's twin-in-residence (.twin/) from the global brainstem.

    Gives every AI already talking to this brainstem the verbs to discover,
    launch, converse with, and stop project twins (see TWIN.md).
    """

    def __init__(self):
        self.name = "TwinConnector"
        self.metadata = {
            "name": self.name,
            "description": (
                "Discover, start, chat with, or stop a repo's twin-in-residence "
                "(.twin/). Use action=discover to list twins, action=up to "
                "launch one, action=chat with a message to converse, "
                "action=down to stop it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["discover", "up", "chat", "down"],
                        "description": "What to do with the twin",
                    },
                    "repo_path": {
                        "type": "string",
                        "description": "Path to the repo carrying .twin/ (not needed for discover)",
                    },
                    "message": {
                        "type": "string",
                        "description": "For action=chat: what to say to the twin",
                    },
                },
                "required": ["action"],
            },
        }
        super().__init__(name=self.name, metadata=self.metadata)

    def _twin_sh(self):
        candidates = [
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "twin.sh"),
            os.path.expanduser("~/.brainstem/src/rapp_brainstem/twin.sh"),
        ]
        for c in candidates:
            c = os.path.abspath(c)
            if os.path.exists(c):
                return c
        return None

    def _manifest(self, repo_path):
        path = os.path.join(os.path.expanduser(repo_path), ".twin", "twin.json")
        if not os.path.exists(path):
            return None
        try:
            return json.load(open(path))
        except Exception:
            return None

    def perform(self, **kwargs):
        action = kwargs.get("action", "")
        repo = os.path.expanduser(kwargs.get("repo_path") or "")

        if action == "discover":
            found = []
            roots = [os.path.expanduser("~/Documents/GitHub"), os.getcwd()]
            for root in roots:
                for mf in glob.glob(os.path.join(root, "*", ".twin", "twin.json")):
                    try:
                        m = json.load(open(mf))
                        found.append(
                            f"{m.get('name', '?')} (port {m.get('port', '?')}) — "
                            f"{os.path.dirname(os.path.dirname(mf))}"
                        )
                    except Exception:
                        continue
            return "twins found:\n" + "\n".join(sorted(set(found))) if found else "no .twin/ found under " + ", ".join(roots)

        if not repo:
            return "repo_path is required for this action"
        manifest = self._manifest(repo)
        if manifest is None:
            return f"no readable .twin/twin.json in {repo}"
        port = manifest.get("port", 7091)

        if action in ("up", "down"):
            tool = self._twin_sh()
            if not tool:
                return "twin.sh not found (repo payload or ~/.brainstem/src)"
            try:
                r = subprocess.run(
                    ["bash", tool, action, repo],
                    capture_output=True, text=True, timeout=300,
                )
                out = (r.stdout + r.stderr).strip()
                return out[-1500:] if out else f"twin.sh {action} exited {r.returncode}"
            except subprocess.TimeoutExpired:
                return f"twin.sh {action} timed out after 300s"

        if action == "chat":
            message = kwargs.get("message", "").strip()
            if not message:
                return "message is required for action=chat"
            req = urllib.request.Request(
                f"http://localhost:{port}/chat",
                data=json.dumps({"user_input": message}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
            except Exception as e:
                return f"twin on :{port} did not answer ({e}) — try action=up first"
            return body.get("response") or json.dumps(body)[:1500]

        return f"unknown action: {action}"
