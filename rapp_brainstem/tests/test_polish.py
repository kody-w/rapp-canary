"""
Regression tests for the pearl-polish pass. Each test guards one specific fix so it
can't silently regress. Hermetic: no network, no real token or state files touched.

    python3 -m pytest test_polish.py -v
"""
import json
import os
import pytest

import brainstem as bs
import local_storage


# ── Security: the static route no longer serves the brainstem directory ────────

def test_static_route_does_not_serve_dotfiles_or_source():
    """Regression for the Flask static_folder leak: a GET of any brainstem file
    (.env with GITHUB_TOKEN, the token caches, the source) must NOT be served."""
    c = bs.app.test_client()
    for path in ("/rapp_brainstem/.env", "/rapp_brainstem/.copilot_token",
                 "/rapp_brainstem/.copilot_session", "/rapp_brainstem/brainstem.py",
                 "/static/.env", "/.env"):
        assert c.get(path).status_code == 404, f"{path} should not be served"


def test_index_html_still_served():
    r = bs.app.test_client().get("/")
    assert r.status_code == 200 and b"RAPP Brainstem" in r.data


# ── RAR browser fetches the primary registry and protects shared dependencies ─

def test_rar_fetch_uses_raw_github_before_mirror():
    index = open(os.path.join(bs._BASE_DIR, "index.html"), encoding="utf-8").read()
    helper = index[index.index("async function rarFetch"):index.index("let rarRegistry")]
    assert "fetch(encodeURI(`${RAR_BASE}/${path}`))" in helper
    assert "await rarFetch(path)" not in helper


def test_rar_browser_does_not_offer_basic_agent_for_install():
    index = open(os.path.join(bs._BASE_DIR, "index.html"), encoding="utf-8").read()
    helper = index[index.index("function isLoadableAgent"):index.index("function loadRarRegistry")]
    assert "base !== 'basic_agent.py'" in helper


def test_rar_browser_uses_collision_safe_install_filename():
    index = open(os.path.join(bs._BASE_DIR, "index.html"), encoding="utf-8").read()
    helper = index[index.index("async function installRarAgent"):index.index("async function copyDeviceCode")]
    assert "agent._install_filename ||" in helper
    assert "filename.includes('/')" in helper


def test_stream_fallback_stops_after_response_is_accepted():
    index = open(os.path.join(bs._BASE_DIR, "index.html"), encoding="utf-8").read()
    send = index[index.index("async function sendMessage"):index.index("async function sendViaPost")]
    stream = index[index.index("async function sendViaStream"):index.index("// ── Voice")]
    assert "err.streamAccepted" in send
    assert "streamed = true" in send
    assert "responseAccepted = true" in stream
    assert "err.streamAccepted = true" in stream


def test_launchers_probe_all_runtime_dependencies_and_use_python_m_pip():
    root = bs._BASE_DIR
    powershell = open(os.path.join(root, "start.ps1"), encoding="utf-8").read()
    shell = open(os.path.join(root, "start.sh"), encoding="utf-8").read()
    for launcher in (powershell, shell):
        assert "pyzipper" in launcher
        assert "-m pip" in launcher
    assert "$managedPython" in powershell
    assert "@($env:Path, $machinePath, $userPath)" in powershell


# ── /chat input validation always returns JSON (never an HTML 400/500) ─────────

def test_chat_rejects_non_json_body_as_json():
    r = bs.app.test_client().post("/chat", data="{ not json",
                                  content_type="application/json")
    assert r.status_code == 400 and r.is_json and "error" in r.get_json()


def test_chat_rejects_non_string_user_input():
    r = bs.app.test_client().post("/chat", json={"user_input": 123})
    assert r.status_code == 400 and r.get_json()["error"]


def test_chat_requires_non_empty_user_input():
    r = bs.app.test_client().post("/chat", json={"user_input": "   "})
    assert r.status_code == 400


@pytest.mark.parametrize("history", [[None], ["bad"], [{"role": "user", "content": 7}]])
def test_chat_rejects_malformed_history_as_json(history):
    r = bs.app.test_client().post(
        "/chat", json={"user_input": "hi", "conversation_history": history})
    assert r.status_code == 400 and r.is_json and "conversation_history" in r.get_json()["error"]


def test_stream_rejects_malformed_history_as_json():
    r = bs.app.test_client().post(
        "/chat/stream", json={"user_input": "hi", "conversation_history": [None]})
    assert r.status_code == 400 and r.is_json


# ── DELETE cannot remove the shared base class ─────────────────────────────────

def test_cannot_delete_basic_agent():
    r = bs.app.test_client().delete("/agents/basic_agent.py")
    assert r.status_code == 400
    base = os.path.join(bs._BASE_DIR, "agents", "basic_agent.py")
    assert os.path.exists(base), "basic_agent.py must remain"


def test_cannot_replace_basic_agent(tmp_path, monkeypatch):
    from io import BytesIO

    base = tmp_path / "basic_agent.py"
    base.write_text("sentinel", encoding="utf-8")
    monkeypatch.setattr(bs, "AGENTS_PATH", str(tmp_path))
    r = bs.app.test_client().post(
        "/agents/import",
        data={"file": (BytesIO(b"print('should not run')"), "basic_agent.py")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 400
    assert base.read_text(encoding="utf-8") == "sentinel"


def test_loader_ignores_imported_class_alias(tmp_path):
    source = tmp_path / "alias_agent.py"
    source.write_text(
        "from agents.basic_agent import BasicAgent as Parent\n"
        "class LocalAgent(Parent):\n"
        "    def __init__(self):\n"
        "        self.name = 'Local'\n"
        "        self.metadata = {'name': 'Local', 'description': 'local', "
        "'parameters': {'type': 'object', 'properties': {}}}\n"
        "        super().__init__(self.name, self.metadata)\n"
        "    def perform(self, **kwargs):\n"
        "        return 'ok'\n",
        encoding="utf-8",
    )
    assert list(bs._load_agent_from_file(str(source))) == ["Local"]


# ── call_copilot: an empty "choices" array is a clean error, not an IndexError ──

def test_call_copilot_empty_choices_raises_runtimeerror(monkeypatch):
    monkeypatch.setattr(bs, "get_copilot_token", lambda: ("tok", "https://ep"))

    class FakeResp:
        status_code = 200
        text = "{}"
        encoding = "utf-8"
        def raise_for_status(self):
            pass
        def json(self):
            return {"choices": []}

    monkeypatch.setattr(bs.requests, "post", lambda *a, **k: FakeResp())
    with pytest.raises(RuntimeError):
        bs.call_copilot([{"role": "user", "content": "hi"}])


# ── Atomic JSON write helper leaves no temp files and round-trips ──────────────

def test_atomic_write_json_roundtrip(tmp_path):
    p = str(tmp_path / "state.json")
    bs._atomic_write_json(p, {"a": 1, "b": [2, 3]})
    assert json.load(open(p, encoding="utf-8")) == {"a": 1, "b": [2, 3]}
    assert os.listdir(tmp_path) == ["state.json"]  # no leftover .tmp


def test_atomic_binary_failure_preserves_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "agent.py"
    path.write_bytes(b"previous complete file")

    def fail_replace(source, destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(bs.os, "replace", fail_replace)
    with pytest.raises(OSError):
        bs._atomic_write_bytes(str(path), b"partial replacement")

    assert path.read_bytes() == b"previous complete file"
    assert os.listdir(tmp_path) == ["agent.py"]


# ── Relative SOUL/AGENTS paths resolve against the brainstem dir, not the CWD ──

def test_relative_paths_resolve_under_base():
    assert bs._resolve_under_base("./soul.md", "soul.md") == os.path.join(bs._BASE_DIR, "./soul.md")
    assert bs._resolve_under_base(None, "agents") == os.path.join(bs._BASE_DIR, "agents")
    absolute_path = os.path.abspath(os.path.join(os.sep, "abs", "s.md"))
    assert bs._resolve_under_base(absolute_path, "soul.md") == absolute_path


# ── local_storage: traversal containment + bare-filename safety ────────────────

def test_storage_blocks_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(local_storage, "_DATA_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        local_storage._safe_join("../../etc/passwd")
    m = local_storage.AzureFileStorageManager()
    m.set_memory_context("../../escape")
    with pytest.raises(ValueError):
        m.write_json({"x": 1})


def test_storage_blocks_symlink_escape(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    outside = tmp_path / "outside"
    data_dir.mkdir()
    outside.mkdir()
    link = data_dir / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable on this host")

    monkeypatch.setattr(local_storage, "_DATA_DIR", str(data_dir))
    with pytest.raises(ValueError):
        local_storage._safe_join("linked", "escaped.json")


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
def test_storage_files_are_private_on_posix(tmp_path, monkeypatch):
    import stat

    monkeypatch.setattr(local_storage, "_DATA_DIR", str(tmp_path))
    manager = local_storage.AzureFileStorageManager()
    manager.write_json({"secret": True})
    assert stat.S_IMODE(os.stat(tmp_path).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(manager._file_path()).st_mode) == 0o600


def test_storage_bare_filename_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(local_storage, "_DATA_DIR", str(tmp_path))
    m = local_storage.AzureFileStorageManager()
    m.write_json({"k": 1}, file_path="bare.json")   # dirname("") no longer crashes
    assert m.read_json(file_path="bare.json") == {"k": 1}


# ── Memory recall tolerates a corrupted (non-dict) store instead of crashing ───

def test_context_memory_tolerates_corrupt_store(tmp_path, monkeypatch):
    monkeypatch.setattr(local_storage, "_DATA_DIR", str(tmp_path))
    agents_dir = os.path.join(bs._BASE_DIR, "agents")
    ctx = bs._load_agent_from_file(os.path.join(agents_dir, "context_memory_agent.py"))["ContextMemory"]
    with open(ctx.storage_manager._file_path(), "w", encoding="utf-8") as f:
        f.write("[1, 2, 3]")     # a JSON array, not the expected object
    out = ctx.perform(full_recall=True)   # must not raise
    assert isinstance(out, str)
