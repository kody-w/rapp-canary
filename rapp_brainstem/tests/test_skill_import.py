"""Explicitly remembered Markdown uses LearnNew's factory and the Python import path."""

import ast
import importlib.util
import io
import json
import os
from pathlib import Path
import py_compile
import sys

import pytest

import brainstem as bs
import local_storage
from rapp_adapters import skills as skill_adapter


SKILL = """---
name: workshop-helper
description: Guide a workshop without publishing it.
---

# Workshop Helper

Use ALPHA as the workshop label. Stop at Draft. Never publish.
"""
INSTRUCTIONS = "# Workshop Helper\n\nUse ALPHA as the workshop label. Stop at Draft. Never publish."
AGENT_FILE = "workshop_helper_agent.py"
TOOL_NAME = "WorkshopHelper"
PYTHON_AGENT = '''
from agents.basic_agent import BasicAgent

class PythonAgent(BasicAgent):
    def __init__(self):
        super().__init__(name="PythonTool", metadata={
            "name": "PythonTool",
            "description": "An existing Python agent.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        })

    def perform(self, **kwargs):
        return "ALPHA"
'''


@pytest.fixture
def generation(monkeypatch):
    calls = []

    def complete(messages, tools=None):
        assert tools is None
        calls.append(messages)
        label = "BRAVO" if "BRAVO" in messages[-1]["content"] else "ALPHA"
        body = f'''        if kwargs.get("fail"):
            return json.dumps({{"status": "error", "message": "Requested failure"}})
        result = {{"label": "{label}", "query": query,
                  "extra": kwargs.get("extra"), "enabled": kwargs.get("enabled")}}
        return json.dumps({{"status": "success", "result": result}})
'''
        return {"choices": [{"message": {"content": "```python\n" + body + "```"}}]}, "test-model"

    monkeypatch.setattr(bs, "call_copilot", complete)
    return calls


@pytest.fixture
def client(tmp_path, monkeypatch, generation):
    agents_path = tmp_path / "agents"
    agents_path.mkdir()
    monkeypatch.setattr(bs, "AGENTS_PATH", str(agents_path))
    skills_path = tmp_path / "skills"
    skills_path.mkdir()
    monkeypatch.setattr(bs, "SKILLS_PATH", str(skills_path), raising=False)
    monkeypatch.setattr(bs, "_tlog", lambda *args, **kwargs: None)
    monkeypatch.setattr(bs, "get_github_token", lambda: None)
    monkeypatch.setattr(bs, "_load_copilot_cache", lambda: None)

    def no_install(package):
        pytest.fail(f"Import must not install packages: {package}")

    monkeypatch.setattr(bs, "_auto_install", no_install)
    return bs.app.test_client()


def upload(client, content=SKILL, filename="SKILL.md", mode="remember"):
    data = content.encode("utf-8") if isinstance(content, str) else content
    form = {"file": (io.BytesIO(data), filename)}
    if mode is not None:
        form["mode"] = mode
    return client.post(
        "/skills/import" if Path(filename).suffix.lower() == ".md" else "/agents/import",
        data=form,
        content_type="multipart/form-data",
    )


def skill_constant(path, name):
    for node in ast.parse(path.read_bytes()).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return ast.literal_eval(node.value)
    pytest.fail(f"Missing {name} in exported agent")


def agent_files():
    return sorted(path.name for path in Path(bs.AGENTS_PATH).iterdir() if path.name != "__pycache__")


@pytest.mark.parametrize("filename", [
    "SKILL.md", "skill.md", "skills.md", "SKILL (17).md", "workshop.MD",
])
def test_markdown_hot_imports_as_a_normal_agent(client, generation, filename):
    response = upload(client, filename=filename)
    assert response.status_code == 200
    assert response.json["status"] == "ok"
    assert response.json["filename"] == AGENT_FILE
    assert response.json["agents"] == [TOOL_NAME]
    assert agent_files() == [AGENT_FILE]

    agent = bs.load_agents()[TOOL_NAME]
    tool = agent.to_tool()
    assert tool["type"] == "function"
    assert tool["function"]["name"] == TOOL_NAME
    assert tool["function"]["description"] == "Guide a workshop without publishing it."
    assert "query" in tool["function"]["parameters"]["properties"]
    assert agent.system_context() is None
    result = json.loads(agent.perform(query="Build my workshop", extra="a \u96ea value", enabled=True))
    assert result == {
        "status": "success",
        "result": {"label": "ALPHA", "query": "Build my workshop", "extra": "a \u96ea value", "enabled": True},
    }
    assert json.loads(agent.perform(fail=True))["status"] == "error"
    assert len(generation) == 1
    assert SKILL in generation[0][-1]["content"]
    assert "not a wrapper" in generation[0][-1]["content"]
    assert client.get("/health").json["agents"] == [TOOL_NAME]


@pytest.mark.parametrize("filename,content,expected", [
    ("SKILL.md", "# Release Notes\n\nWrite release notes.", "ReleaseNotes"),
    ("skills.md", "# Release Notes\n\nWrite release notes.", "ReleaseNotes"),
    ("Quarterly Review.md", "Summarize the results.", "QuarterlyReview"),
])
def test_plain_markdown_has_a_stable_name(client, generation, filename, content, expected):
    response = upload(client, content, filename)
    assert response.json["status"] == "ok"
    assert expected in bs.load_agents()
    assert content in generation[0][-1]["content"]
    assert skill_constant(Path(bs.AGENTS_PATH) / response.json["filename"], "SKILL_MD") == content


def test_server_startup_does_not_require_yaml(monkeypatch):
    monkeypatch.setitem(sys.modules, "yaml", None)
    spec = importlib.util.spec_from_file_location("brainstem_without_yaml", bs.__file__)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    assert module.app.test_client().get("/version").status_code == 200


def test_learnnew_is_bundled_and_uses_the_full_skill_instead_of_the_description(client, generation):
    path = os.path.join(bs._BASE_DIR, "agents", "learn_new_agent.py")
    learner = bs._load_agent_from_file(path)["LearnNew"]
    assert "skill_md" in learner.metadata["parameters"]["properties"]
    preview = json.loads(learner.perform(
        action="preview", skill_md=SKILL, description="DO NOT LEARN THIS REQUEST",
        skill_filename="SKILL (17).md", output_dir=bs.AGENTS_PATH,
    ))
    assert preview["status"] == "ok"
    assert preview["generator"] == "learnnew-skill"
    assert preview["filename"] == AGENT_FILE
    assert SKILL in generation[0][-1]["content"]
    assert "DO NOT LEARN THIS REQUEST" not in generation[0][-1]["content"]
    assert agent_files() == []


def test_default_discovery_includes_learnnew(client, monkeypatch, tmp_path):
    monkeypatch.setattr(bs, "AGENTS_PATH", os.path.join(bs._BASE_DIR, "agents"))
    monkeypatch.setattr(local_storage, "_DATA_DIR", str(tmp_path / "storage"))
    assert "LearnNew" in bs.load_agents()


def test_description_based_learning_still_uses_the_same_factory(client, generation):
    path = os.path.join(bs._BASE_DIR, "agents", "learn_new_agent.py")
    learner = bs._load_agent_from_file(path)["LearnNew"]
    description = "Return the ALPHA label and the supplied query."
    preview = json.loads(learner.perform(
        action="preview", description=description, name="Ordinary", source="scratch",
        output_dir=bs.AGENTS_PATH,
    ))
    assert preview["status"] == "ok"
    assert preview["generator"] == "builtin-scratch"
    assert preview["filename"] == "ordinary_agent.py"
    assert description in generation[0][-1]["content"]
    assert "SKILL_MD" not in preview["code"]
    assert upload(client, preview["code"], preview["filename"]).json["status"] == "ok"
    assert json.loads(bs.load_agents()["Ordinary"].perform(query="hello"))["result"]["query"] == "hello"


def test_direct_learnnew_skill_creation_and_hot_replacement(client, monkeypatch):
    path = os.path.join(bs._BASE_DIR, "agents", "learn_new_agent.py")
    learner = bs._load_agent_from_file(path)["LearnNew"]
    monkeypatch.setitem(sys.modules, "agents.workshop_helper_agent", None)
    for label in ("ALPHA", "BRAVO"):
        result = json.loads(learner.perform(
            action="remember", skill_md=SKILL.replace("ALPHA", label), output_dir=bs.AGENTS_PATH,
        ))
        assert result["status"] == "success"
        assert result["hot_loaded"] is True
        assert result["filename"] == AGENT_FILE
        assert json.loads(bs.load_agents()[TOOL_NAME].perform())["result"]["label"] == label


def test_hosted_learnnew_does_not_need_the_copilot_cli(client, monkeypatch):
    path = os.path.join(bs._BASE_DIR, "agents", "learn_new_agent.py")
    learner = bs._load_agent_from_file(path)["LearnNew"]

    def no_cli(*args, **kwargs):
        pytest.fail("Hosted LearnNew must use Brainstem's completion client")

    monkeypatch.setattr(bs.subprocess, "run", no_cli)
    result = json.loads(learner.perform(
        action="preview", description="Return a label for the supplied query",
        source="scratch", output_dir=bs.AGENTS_PATH,
    ))
    assert result["status"] == "ok"


def test_skill_learning_never_falls_back_to_a_placeholder(client, monkeypatch):
    path = os.path.join(bs._BASE_DIR, "agents", "learn_new_agent.py")
    learner = bs._load_agent_from_file(path)["LearnNew"]
    learner.set_completion_client(None)
    monkeypatch.setenv("RAPP_LEARN_NO_LLM", "1")
    with pytest.raises(RuntimeError, match="disabled"):
        learner.perform(action="preview", skill_md=SKILL, output_dir=bs.AGENTS_PATH)
    assert agent_files() == []


def test_missing_learnnew_is_an_explicit_import_error(client, monkeypatch):
    monkeypatch.setitem(bs.app.extensions["rapp.skills"], "factory", None)
    response = upload(client)
    assert response.status_code == 503
    assert "LearnNew could not load" in response.json["error"]
    assert agent_files() == []


@pytest.mark.parametrize("failure,status", [("empty", 503), ("syntax", 503), ("network", 502)])
def test_generation_failure_does_not_replace_the_previous_agent(client, monkeypatch, failure, status):
    assert upload(client).json["status"] == "ok"
    path = Path(bs.AGENTS_PATH) / AGENT_FILE
    original = path.read_bytes()

    def fail(messages, tools=None):
        if failure == "network":
            raise bs.requests.exceptions.Timeout("Model connection timed out")
        body = "" if failure == "empty" else "if:\n    return missing"
        return {"choices": [{"message": {"content": body}}]}, "test-model"

    monkeypatch.setattr(bs, "call_copilot", fail)
    response = upload(client, SKILL.replace("ALPHA", "BRAVO"))
    assert response.status_code == status
    assert response.json["error"]
    assert path.read_bytes() == original
    assert agent_files() == [AGENT_FILE]


def test_folded_frontmatter_and_other_metadata(client, generation):
    content = SKILL.replace(
        "description: Guide a workshop without publishing it.",
        "description: >-\n"
        "  Guide a workshop.\n"
        "  Keep it in Draft.\n"
        "license: MIT\n"
        "metadata:\n"
        "  category: workshops\n"
        "allowed-tools:\n"
        "  - a-tool-that-is-not-installed",
    )
    assert upload(client, content).json["status"] == "ok"
    agent = bs.load_agents()[TOOL_NAME]
    assert agent.metadata["description"] == "Guide a workshop. Keep it in Draft."
    assert content in generation[0][-1]["content"]
    assert json.loads(agent.perform(query="Use it"))["status"] == "success"
    assert list(bs.load_agents()) == [TOOL_NAME]


def test_source_is_preserved_as_data_and_sent_intact_to_learnnew(client, generation):
    content = "\ufeff" + SKILL.replace("\n", "\r\n")
    content += '\r\n```python\r\nraise AssertionError("must not execute")\r\n```\r\n'
    content += "\r\n\"\"\" quotes ''' braces {} backslash \\\\ and \u96ea\r\n"
    assert upload(client, content).json["status"] == "ok"
    path = Path(bs.AGENTS_PATH) / AGENT_FILE
    assert skill_constant(path, "SKILL_MD") == content
    assert content in generation[0][-1]["content"]
    assert json.loads(bs.load_agents()[TOOL_NAME].perform(query="Read the skill"))["status"] == "success"


@pytest.mark.parametrize("content", [
    "",
    " \r\n\t ",
    "---\nname: workshop-helper\n",
    "---\n- not-a-mapping\n---\nInstructions",
    "---\nname: [broken\n---\nInstructions",
    "---\nname: true\n---\nInstructions",
    "---\nname: ../../escape\n---\nInstructions",
    "---\nname: NOT-LOWERCASE\n---\nInstructions",
    "---\nname: a--b\n---\nInstructions",
    "---\nname: " + "a" * 65 + "\n---\nInstructions",
    "---\nname: empty-body\n---\n",
    "---\nname: invalid-description\ndescription: []\n---\nInstructions",
    "---\nname: invalid-description\ndescription: ''\n---\nInstructions",
    "---\nname: invalid-description\ndescription: " + "a" * 1025 + "\n---\nInstructions",
    "---\nname: !!python/object/apply:os.system ['not-a-command']\n---\nInstructions",
    b"\xff\xfeinvalid utf-8",
])
def test_invalid_skills_are_rejected_without_saving(client, generation, content):
    response = upload(client, content)
    assert response.status_code == 400
    assert response.json["error"]
    assert agent_files() == []
    assert generation == []


def test_same_upload_filename_does_not_merge_different_skills(client):
    assert upload(client).json["status"] == "ok"
    second = upload(client, SKILL.replace("workshop-helper", "another-workshop"))
    assert second.json["status"] == "ok"
    assert second.json["filename"] == "another_workshop_agent.py"
    assert set(bs.load_agents()) == {TOOL_NAME, "AnotherWorkshop"}


def test_skill_cannot_replace_a_handwritten_python_agent(client):
    path = Path(bs.AGENTS_PATH) / AGENT_FILE
    path.write_text(PYTHON_AGENT, encoding="utf-8")
    response = upload(client)
    assert response.status_code == 409
    assert "already exists" in response.json["error"]
    assert path.read_text(encoding="utf-8") == PYTHON_AGENT
    assert bs.load_agents()["PythonTool"].perform() == "ALPHA"


@pytest.mark.parametrize("filename,content", [
    ("basic.py", PYTHON_AGENT),
    ("basic_agent.py", PYTHON_AGENT),
    ("Basic_agent.PY", PYTHON_AGENT),
    ("SKILL.md", SKILL.replace("workshop-helper", "basic")),
])
def test_import_cannot_replace_the_shared_base_class(client, filename, content):
    response = upload(client, content, filename)
    assert response.status_code == 400
    assert "shared base class" in response.json["error"] or filename.endswith(".PY")
    assert agent_files() == []


def test_skill_cannot_replace_its_own_learner(client, generation):
    response = upload(client, SKILL.replace("workshop-helper", "learn-new"))
    assert response.status_code == 400
    assert "bundled learning agent" in response.json["error"]
    assert generation == []
    assert agent_files() == []


def test_bad_update_preserves_the_installed_skill(client):
    assert upload(client).json["status"] == "ok"
    path = Path(bs.AGENTS_PATH) / AGENT_FILE
    original = path.read_bytes()
    response = upload(client, SKILL.replace("name: workshop-helper", "name: [broken"))
    assert response.status_code == 400
    assert path.read_bytes() == original
    assert json.loads(bs.load_agents()[TOOL_NAME].perform())["result"]["label"] == "ALPHA"


def test_failed_write_preserves_the_previous_agent_and_cleans_temp_files(client, monkeypatch):
    assert upload(client).json["status"] == "ok"
    path = Path(bs.AGENTS_PATH) / AGENT_FILE
    original = path.read_bytes()

    def fail_replace(source, destination):
        raise OSError("read-only destination")

    monkeypatch.setattr(bs.os, "replace", fail_replace)
    response = upload(client, SKILL.replace("ALPHA", "BRAVO"))
    assert response.status_code == 500
    assert "read-only destination" in response.json["error"]
    assert path.read_bytes() == original
    assert agent_files() == [AGENT_FILE]


def test_unloadable_generated_agent_does_not_replace_a_working_one(client, monkeypatch):
    assert upload(client).json["status"] == "ok"
    path = Path(bs.AGENTS_PATH) / AGENT_FILE
    original = path.read_bytes()
    real_load = bs._load_agent_from_file

    def load(filepath):
        return {} if os.path.basename(filepath).startswith(".import-") else real_load(filepath)

    monkeypatch.setattr(bs, "_load_agent_from_file", load)
    response = upload(client, SKILL.replace("ALPHA", "BRAVO"))
    assert response.status_code == 503
    assert "not replaced" in response.json["error"]
    assert path.read_bytes() == original
    assert agent_files() == [AGENT_FILE]


@pytest.mark.parametrize("filename,content,agent_name", [
    ("SKILL.md", SKILL, TOOL_NAME),
    ("python.py", PYTHON_AGENT, "PythonTool"),
])
def test_same_size_same_timestamp_hot_reload_ignores_stale_bytecode(
    client, filename, content, agent_name,
):
    first = upload(client, content, filename)
    path = Path(bs.AGENTS_PATH) / first.json.get("filename", "python_agent.py")
    assert "ALPHA" in bs.load_agents()[agent_name].perform()
    original_stat = path.stat()
    py_compile.compile(str(path), doraise=True)
    updated = content.replace("ALPHA", "BRAVO")
    assert upload(client, updated, filename).json["status"] == "ok"
    assert path.stat().st_size == original_stat.st_size
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    result = bs.load_agents()[agent_name].perform()
    assert "BRAVO" in result
    assert "ALPHA" not in result


def test_list_export_reimport_and_delete_share_the_python_agent_lifecycle(client, tmp_path, monkeypatch):
    assert upload(client).json["status"] == "ok"
    listing = client.get("/agents").json["files"]
    assert listing == [{"filename": AGENT_FILE, "agents": [TOOL_NAME]}]
    exported = client.get(f"/agents/export/{AGENT_FILE}")
    assert exported.status_code == 200
    assert AGENT_FILE in exported.headers["Content-Disposition"]
    assert exported.data == (Path(bs.AGENTS_PATH) / AGENT_FILE).read_bytes()
    exported_data = exported.data
    exported.close()

    assert client.delete(f"/agents/{AGENT_FILE}").json["status"] == "ok"
    assert bs.load_agents() == {}
    assert client.get("/agents").json["files"] == []

    monkeypatch.setattr(bs, "AGENTS_PATH", str(tmp_path / "other-instance"))
    assert upload(client, exported_data, AGENT_FILE).json["status"] == "ok"
    assert json.loads(bs.load_agents()[TOOL_NAME].perform(query="Use the exported agent"))["status"] == "success"
    assert agent_files() == [AGENT_FILE]
    assert upload(client, SKILL.replace("ALPHA", "BRAVO")).json["status"] == "ok"
    assert "BRAVO" in bs.load_agents()[TOOL_NAME].perform()


def test_chat_executes_the_learned_python_tool_without_reinjecting_the_skill(client, monkeypatch):
    assert upload(client).json["status"] == "ok"
    monkeypatch.setattr(bs, "load_soul", lambda: "You are the host assistant.")
    monkeypatch.setattr(bs, "VOICE_MODE", False)
    calls = []

    def fake_copilot(messages, tools=None):
        calls.append(len(messages))
        assert tools[0]["function"]["name"] == TOOL_NAME
        if len(calls) == 1:
            assert messages[0] == {"role": "system", "content": "You are the host assistant."}
            assert INSTRUCTIONS not in json.dumps(messages + tools)
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_skill",
                    "type": "function",
                    "function": {
                        "name": TOOL_NAME,
                        "arguments": json.dumps({"query": "Build a workshop"}),
                    },
                }],
            }
        else:
            assert len(calls) == 2
            result = messages[-1]
            assert result["role"] == "tool"
            assert result["tool_call_id"] == "call_skill"
            data = json.loads(result["content"])
            assert data["result"]["label"] == "ALPHA"
            assert data["result"]["query"] == "Build a workshop"
            assert INSTRUCTIONS not in result["content"]
            message = {"role": "assistant", "content": "The label is ALPHA."}
        return {"choices": [{"message": message}]}, "test-model"

    monkeypatch.setattr(bs, "call_copilot", fake_copilot)
    response = client.post("/chat", json={"user_input": "Use workshop-helper to build a workshop"})
    assert response.status_code == 200
    assert response.json["response"] == "The label is ALPHA."
    assert TOOL_NAME in response.json["agent_logs"]
    assert len(calls) == 2


def test_unsupported_upload_stays_rejected(client):
    response = upload(client, "not a skill", "notes.txt")
    assert response.status_code == 400
    assert ".py" in response.json["error"]


def test_broken_python_upload_stays_visible_for_removal(client):
    response = upload(client, "# No agent class", "broken.py")
    assert "error" in response.json
    assert client.get("/agents").json["files"] == [{"filename": "broken_agent.py", "agents": []}]
    assert client.delete("/agents/broken_agent.py").status_code == 200
