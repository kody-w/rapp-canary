#!/usr/bin/env python3
"""Verify actual Astra selection, a response, and a streamed tool round-trip without fallback."""

import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
import uuid


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests
from werkzeug.test import Client
from werkzeug.wrappers import Response

from kernel_compat import KERNEL_SHA256, bind_providers, load_kernel, load_runtime_profile
from provider_host import load_registry
from rapp_adapters.rapp1 import rapp as R


class ObservedHTTP:
    """Record only routing facts; all responses come from the real provider."""
    def __init__(self):
        self.inference = []
        self.metadata_events = []

    def __getattr__(self, name):
        return getattr(requests, name)

    def post(self, url, **kwargs):
        path = urlsplit(url).path
        if path in ("/responses", "/chat/completions"):
            self.inference.append({"endpoint": path, "model": kwargs.get("json", {}).get("model")})
        response = requests.post(url, **kwargs)
        if path == "/responses" and response.status_code == 200:
            original_lines = response.iter_lines

            def observed_lines(*args, **options):
                for line in original_lines(*args, **options):
                    text = line.decode("utf-8") if isinstance(line, bytes) else line
                    if text.startswith("data: "):
                        try:
                            event = json.loads(text[6:])
                        except ValueError:
                            event = None
                        if isinstance(event, dict) and isinstance(event.get("response"), dict):
                            self.metadata_events.append({
                                "type": event.get("type"),
                                "id_sha256": hashlib.sha256(str(event["response"].get("id", "")).encode()).hexdigest(),
                                **{key: event["response"].get(key) for key in ("model", "created_at", "status")},
                            })
                    yield line

            response.iter_lines = observed_lines
        return response


class LocalClient(Client):
    def open(self, *args, **kwargs):
        kwargs.setdefault("environ_overrides", {})["REMOTE_ADDR"] = "127.0.0.1"
        return super().open(*args, **kwargs)


def verify(output, token_file):
    token_data = json.loads(token_file.read_text(encoding="utf-8"))
    access = token_data.get("access_token")
    if not isinstance(access, str) or not access:
        raise RuntimeError("The documented Brainstem login has no usable access token.")
    nonce = "ASTRA_PROBE_" + uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix="astra-live-") as directory:
        temporary = Path(directory)
        agents = temporary / "agents"
        agents.mkdir()
        shutil.copyfile(Path(__file__).with_name("fixtures") / "astra_probe_agent.py", agents / "astra_probe_agent.py")
        soul = temporary / "soul.md"
        soul.write_text("Complete the user's bounded acceptance probe. Use only the supplied echo tool.", encoding="utf-8")
        trace = temporary / "tool-trace.jsonl"
        os.environ.update({
            "GITHUB_TOKEN": access, "GITHUB_MODEL": "gpt-6-astra", "BRAINSTEM_LAN_MODE": "false",
            "SOUL_PATH": str(soul), "AGENTS_PATH": str(agents), "ASTRA_PROBE_TRACE": str(trace),
        })
        with contextlib.redirect_stdout(io.StringIO()):
            snapshot = load_kernel(ROOT / "brainstem.py")
            kernel = snapshot.module
            kernel._token_file = str(temporary / "unused-token.json")
            kernel._copilot_cache_file = str(temporary / "session.json")
            kernel._model_file = str(temporary / "selected-model.json")
            observed = ObservedHTTP()
            kernel.requests = observed
            registry = load_registry(ROOT / "provider_plugins/plugins.json", selection="responses")
            application = bind_providers(snapshot, registry, profile=load_runtime_profile(ROOT / "runtime_profile.json"))
            client = LocalClient(application, Response)
            models = client.get("/models").get_json()
            assert any(model["id"] == "gpt-6-astra" for model in models["models"]), "Astra is not in the adapted catalog"
            selected = client.post("/models/set", json={"model": "gpt-6-astra"})
            assert selected.status_code == 200, "Astra selection failed"
            reply = client.post("/chat", json={"user_input": f"Reply with exactly {nonce}. Do not use tools."})
            answer = reply.get_json()
            assert reply.status_code == 200 and "error" not in answer, (
                "Astra text request failed: " + str(answer.get("error", reply.status_code))
                + "; safe response metadata: " + json.dumps(observed.metadata_events)
            )
            assert answer["model"] == answer["requested_model"] == "gpt-6-astra", "A fallback model answered"
            assert nonce in answer["response"], "Astra did not answer the acceptance probe"
            trace.unlink(missing_ok=True)
            stream = client.post("/chat/stream", json={
                "user_input": f"Call AstraProbeEcho exactly once with value {nonce}_TOOL. Then return the tool's output.",
            })
            events = [json.loads(line[6:]) for line in stream.get_data(as_text=True).splitlines()
                      if line.startswith("data: ")]
            assert events and events[-1]["type"] == "done", "Astra stream did not complete"
            done = events[-1]
            assert done["model"] == done["requested_model"] == "gpt-6-astra", "A fallback model answered the tool probe"
            assert done["streamed"] is True and any(event["type"] == "delta" for event in events)
            assert nonce + "_TOOL" in done["response"], "Astra did not consume the actual tool output"
            calls = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
            assert calls == [{"value": nonce + "_TOOL"}], (
                "The stream probe did not execute the requested tool exactly once: " + json.dumps(calls)
            )
            assert observed.inference and all(
                call == {"endpoint": "/responses", "model": "gpt-6-astra"} for call in observed.inference
            ), "A live provider call fell back to Chat Completions or another model"
            assert reply.headers["X-Brainstem-Kernel-SHA256"] == KERNEL_SHA256
        evidence = {
            "requested_model": "gpt-6-astra", "actual_model": answer["model"],
            "stream_actual_model": done["model"], "fallback": False, "streamed": True,
            "tool_calls_executed": len(calls), "provider_requests": observed.inference,
            "kernel_sha256": snapshot.sha256, "provider_api": 1,
            "reply_sha256": hashlib.sha256(answer["response"].encode()).hexdigest(),
            "stream_reply_sha256": hashlib.sha256(done["response"].encode()).hexdigest(),
        }
    output.mkdir(parents=True, exist_ok=False)
    identity = R.mint_rappid("local", "astra-acceptance")
    frame = R.build_frame(
        "provider.verify", identity, 0,
        datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        evidence, None,
    )
    assert R.verify_frame(frame, stream_id_of_record=identity)[0]
    (output / "frames").mkdir()
    (output / "rappid.json").write_text(R.canonical({"schema": "rapp/1", "rappid": identity}) + "\n", encoding="utf-8")
    (output / "frames/00000000000000000000.json").write_text(R.canonical(frame) + "\n", encoding="utf-8")
    checked = subprocess.run(
        [sys.executable, str(Path(R.__file__).with_name("rapp_check.py")), str(output), "--json"],
        capture_output=True, text=True, check=True,
    )
    report = json.loads(checked.stdout)
    assert report["verdict"] == "COMPLIANT" and any("1 frames conform" in item["ok"] for item in report["evidence"])
    (output / "check.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2))
    print("PASS live Astra text, streamed tool round-trip, no fallback, unchanged kernel, and RAPP/1 evidence")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--token-file", type=Path,
                        default=Path.home() / ".brainstem/src/rapp_brainstem/.copilot_token")
    args = parser.parse_args()
    verify(args.output, args.token_file)
