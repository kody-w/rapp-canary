"""Markdown persists and hot-loads from skills/, independently of Python agents."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import quote

import pytest

import brainstem as bs
from rapp_adapters import skills as skill_adapter
from test_skill_import import AGENT_FILE, PYTHON_AGENT, SKILL, TOOL_NAME, client, generation, upload


SKILL_FILE = "workshop-helper.md"


@pytest.mark.parametrize("filename", ["SKILL.md", "skills.md", "SKILL (17).md", "Workshop.MD"])
@pytest.mark.parametrize("mode", [None, "skill"])
def test_upload_defaults_to_storing_original_markdown(client, generation, filename, mode):
    original = ("\ufeff" + SKILL.replace("\n", "\r\n")).encode("utf-8")
    response = upload(client, original, filename, mode)
    assert response.status_code == 200
    assert response.json["scope"] == "skill"
    assert response.json["filename"] == SKILL_FILE
    assert (Path(bs.SKILLS_PATH) / SKILL_FILE).read_bytes() == original
    assert os.listdir(bs.AGENTS_PATH) == []
    assert generation == []
    assert client.get("/skills").json["files"][0]["name"] == "workshop-helper"
    assert "markdown" not in client.get("/skills").json["files"][0]
    assert client.get("/agents").json["files"] == []


def test_direct_drop_and_edits_hot_load_without_mtime_or_instance_changes(client, generation):
    path = Path(bs.SKILLS_PATH) / "SKILL (17).MD"
    path.write_bytes(SKILL.encode("utf-8"))
    learner = skill_adapter.learner(bs)
    first = json.loads(learner.perform(action="use", name="workshop-helper"))
    assert first["skill"]["markdown"] == SKILL
    original = path.stat()
    updated = SKILL.replace("ALPHA", "BRAVO")
    path.write_bytes(updated.encode("utf-8"))
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert json.loads(learner.perform(action="use", name=path.name))["skill"]["markdown"] == updated
    assert generation == []
    path.unlink()
    assert learner.get_skills() == []
    with pytest.raises(ValueError, match="stored skill"):
        learner.perform(action="use", name="workshop-helper")


def test_using_raw_markdown_stores_it_and_new_instances_read_it(client, generation):
    first = skill_adapter.learner(bs)
    result = json.loads(first.perform(skill_md=SKILL, query="Use this skill"))
    assert result["scope"] == "skill" and result["request"] == "Use this skill"
    assert first.skills_dir == Path(bs.SKILLS_PATH)
    second = skill_adapter.learner(bs)
    assert second is not first
    assert json.loads(second.perform(action="use", name="workshop-helper"))["skill"]["markdown"] == SKILL
    assert generation == []


def test_same_named_upload_updates_a_directly_dropped_file(client):
    path = Path(bs.SKILLS_PATH) / "SKILL.md"
    path.write_bytes(SKILL.encode("utf-8"))
    updated = SKILL.replace("ALPHA", "BRAVO")
    response = upload(client, updated, mode=None)
    assert response.json["filename"] == "SKILL.md"
    assert path.read_bytes() == updated.encode("utf-8")
    assert os.listdir(bs.SKILLS_PATH) == ["SKILL.md"]


@pytest.mark.parametrize("route", ["/chat", "/chat/stream"])
def test_refresh_and_new_chat_discover_metadata_and_load_instructions_on_demand(client, monkeypatch, route):
    upload(client, mode=None)
    monkeypatch.setattr(bs, "load_agents", lambda: {"LearnNew": skill_adapter.learner(bs)})
    prompts = []

    def answer(messages, tools=None):
        prompts.append(messages[:])
        if messages[-1]["role"] != "tool":
            catalog = json.loads(next(line for line in messages[0]["content"].splitlines()
                                      if line.startswith("[") and '"filename"' in line))
            assert catalog[0]["name"] == "workshop-helper"
            assert "markdown" not in catalog[0]
            assert SKILL not in messages[0]["content"]
            assert any(tool["function"]["name"] == "LearnNew" for tool in tools)
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "read-skill", "type": "function",
                "function": {"name": "LearnNew", "arguments": json.dumps({
                    "action": "use", "name": "workshop-helper",
                })},
            }]}
        else:
            assert json.loads(messages[-1]["content"])["skill"]["markdown"] == SKILL
            message = {"role": "assistant", "content": "The stored skill is available."}
        return {"choices": [{"message": message}]}, "test-model"

    monkeypatch.setattr(bs, "call_copilot", answer)

    def stream(messages, tools=None):
        response, model = answer(messages, tools)
        message = response["choices"][0]["message"]
        if message.get("content"):
            yield "delta", message["content"]
        yield "done", {"message": message, "model": model}

    monkeypatch.setattr(bs, "call_copilot_stream", stream)
    for session_id in ("before-refresh", "after-refresh"):
        response = client.post(route, json={"user_input": "Use my workshop skill", "session_id": session_id})
        assert response.status_code == 200
        if route == "/chat":
            assert "session_skills" not in response.json
        else:
            events = [json.loads(line[6:]) for line in response.get_data(as_text=True).splitlines()
                      if line.startswith("data: ")]
            assert events[-1]["type"] == "done"
            assert events[-1]["response"] == "The stored skill is available."
    assert len(prompts) == 4
    assert (Path(bs.SKILLS_PATH) / SKILL_FILE).is_file()
    assert os.listdir(bs.AGENTS_PATH) == []


def test_list_export_delete_work_for_markdown_without_touching_agents(client):
    upload(client, mode=None)
    upload(client, PYTHON_AGENT, "ordinary.py")
    with client.get(f"/skills/export/{SKILL_FILE}") as exported:
        assert exported.status_code == 200
        assert exported.data == SKILL.encode("utf-8")
        assert SKILL_FILE in exported.headers["Content-Disposition"]
    assert client.delete(f"/skills/{SKILL_FILE}").json["status"] == "ok"
    assert client.get("/skills").json["files"] == []
    assert bs.load_agents()["PythonTool"].perform() == "ALPHA"
    assert client.get(f"/skills/export/{SKILL_FILE}").status_code == 404
    assert client.delete(f"/skills/{SKILL_FILE}").status_code == 404


@pytest.mark.parametrize("filename", ["../outside.md", r"..\outside.md", ".hidden.md", "agent.py"])
def test_skill_routes_reject_paths_outside_the_markdown_namespace(client, tmp_path, filename):
    outside = tmp_path / "outside.md"
    outside.write_text("keep this", encoding="utf-8")
    encoded = quote(filename, safe="")
    assert client.get(f"/skills/export/{encoded}").status_code in (400, 404)
    assert client.delete(f"/skills/{encoded}").status_code in (400, 404)
    assert outside.read_text(encoding="utf-8") == "keep this"


def test_symlinks_are_not_read_and_deleting_one_only_removes_the_link(client, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("private outside contents", encoding="utf-8")
    link = Path(bs.SKILLS_PATH) / "linked.md"
    try:
        link.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"Symbolic links unavailable: {error}")
    record = client.get("/skills").json["files"][0]
    assert "Symbolic-link" in record["error"]
    assert client.get("/skills/export/linked.md").status_code == 400
    assert client.delete("/skills/linked.md").status_code == 200
    assert outside.read_text(encoding="utf-8") == "private outside contents"


def test_discovery_is_flat_and_errors_do_not_hide_healthy_skills(client):
    directory = Path(bs.SKILLS_PATH)
    (directory / "nested").mkdir()
    (directory / "nested" / "SKILL.md").write_text(SKILL, encoding="utf-8")
    (directory / ".hidden.md").write_text(SKILL, encoding="utf-8")
    (directory / "invalid.md").write_bytes(b"\xff")
    upload(client, mode=None)
    records = client.get("/skills").json["files"]
    assert {record["filename"] for record in records} == {"invalid.md", SKILL_FILE}
    assert next(record for record in records if record["filename"] == "invalid.md")["error"]
    assert json.loads(skill_adapter.learner(bs).perform(action="use", name="workshop-helper"))["scope"] == "skill"


def test_duplicate_skill_names_are_reported_and_never_overwritten(client):
    for filename in ("one.md", "two.md"):
        (Path(bs.SKILLS_PATH) / filename).write_text(SKILL, encoding="utf-8")
    records = client.get("/skills").json["files"]
    assert len(records) == 2 and all("Duplicate" in record["error"] for record in records)
    response = upload(client, SKILL.replace("ALPHA", "BRAVO"), mode=None)
    assert response.status_code == 409
    assert all(path.read_text(encoding="utf-8") == SKILL for path in Path(bs.SKILLS_PATH).iterdir())
    with pytest.raises(ValueError):
        skill_adapter.learner(bs).perform(action="use", name="workshop-helper")


def test_a_different_skill_in_the_canonical_filename_is_not_replaced(client):
    original = SKILL.replace("workshop-helper", "different-skill")
    path = Path(bs.SKILLS_PATH) / SKILL_FILE
    path.write_text(original, encoding="utf-8")
    assert upload(client, mode=None).status_code == 409
    assert path.read_text(encoding="utf-8") == original


def test_invalid_update_and_write_failure_keep_the_original_markdown(client, monkeypatch):
    upload(client, mode=None)
    path = Path(bs.SKILLS_PATH) / SKILL_FILE
    assert upload(client, SKILL.replace("name: workshop-helper", "name: [broken"), mode=None).status_code == 400
    assert path.read_bytes() == SKILL.encode("utf-8")

    def fail(source, destination):
        raise OSError("storage unavailable")

    monkeypatch.setattr(bs.os, "replace", fail)
    response = upload(client, SKILL.replace("ALPHA", "BRAVO"), mode=None)
    assert response.status_code == 500
    assert path.read_bytes() == SKILL.encode("utf-8")
    assert os.listdir(bs.SKILLS_PATH) == [SKILL_FILE]


@pytest.mark.parametrize("mode", ["agent", "remember"])
def test_explicit_conversion_keeps_the_markdown_source(client, generation, mode):
    upload(client, mode=None)
    response = upload(client, filename=SKILL_FILE, mode=mode)
    assert response.json["scope"] == "persistent"
    assert response.json["filename"] == AGENT_FILE
    assert (Path(bs.SKILLS_PATH) / SKILL_FILE).read_bytes() == SKILL.encode("utf-8")
    assert TOOL_NAME in bs.load_agents()
    assert len(generation) == 1


def test_learnnew_can_convert_a_stored_skill_without_conversation_state(client, monkeypatch):
    upload(client, mode=None)
    monkeypatch.setitem(sys.modules, "agents.workshop_helper_agent", None)
    result = json.loads(skill_adapter.learner(bs).perform(action="convert", name="workshop-helper"))
    assert result["converted"] is True
    assert (Path(bs.AGENTS_PATH) / AGENT_FILE).is_file()
    assert (Path(bs.SKILLS_PATH) / SKILL_FILE).is_file()


@pytest.mark.parametrize("mode", ["session", "unknown"])
def test_obsolete_or_invalid_modes_are_not_silently_persisted(client, generation, mode):
    assert upload(client, mode=mode).status_code == 400
    assert generation == []
    assert os.listdir(bs.SKILLS_PATH) == []


def test_skills_config_resolves_like_agents_config(client):
    assert bs._resolve_under_base("./skills", "skills") == os.path.join(bs._BASE_DIR, "./skills")
    assert bs._resolve_under_base(None, "skills") == os.path.join(bs._BASE_DIR, "skills")


@pytest.mark.parametrize("method,path", [
    ("GET", "/skills"), ("GET", f"/skills/export/{SKILL_FILE}"), ("DELETE", f"/skills/{SKILL_FILE}"),
])
def test_skill_routes_preserve_lan_secret_and_origin_guards(client, monkeypatch, method, path):
    upload(client, mode=None)
    monkeypatch.setattr(bs, "_load_or_create_secret", lambda: "test-secret")
    remote = {"REMOTE_ADDR": "192.0.2.12"}
    assert client.open(path, method=method, environ_base=remote).status_code == 403
    assert client.open(path, method=method, headers={"Origin": "https://foreign.example"}).status_code == 403
    allowed = client.open(path, method=method, environ_base=remote,
                          headers={"X-Brainstem-Secret": "test-secret"})
    assert allowed.status_code == 200
    allowed.close()


def test_stored_skills_browser_lifecycle():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed for the browser regression")
    result = subprocess.run(
        [node, str(Path(__file__).with_name("test_stored_skills_ui.mjs"))],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
