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


# ── DELETE cannot remove the shared base class ─────────────────────────────────

def test_cannot_delete_basic_agent():
    r = bs.app.test_client().delete("/agents/basic_agent.py")
    assert r.status_code == 400
    base = os.path.join(bs._BASE_DIR, "agents", "basic_agent.py")
    assert os.path.exists(base), "basic_agent.py must remain"


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


# ── Relative SOUL/AGENTS paths resolve against the brainstem dir, not the CWD ──

def test_relative_paths_resolve_under_base():
    assert bs._resolve_under_base("./soul.md", "soul.md") == os.path.join(bs._BASE_DIR, "./soul.md")
    assert bs._resolve_under_base(None, "agents") == os.path.join(bs._BASE_DIR, "agents")
    assert bs._resolve_under_base(os.path.join(os.sep, "abs", "s.md"), "soul.md") == os.path.join(os.sep, "abs", "s.md")


# ── local_storage: traversal containment + bare-filename safety ────────────────

def test_storage_blocks_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(local_storage, "_DATA_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        local_storage._safe_join("../../etc/passwd")
    m = local_storage.AzureFileStorageManager()
    m.set_memory_context("../../escape")
    with pytest.raises(ValueError):
        m.write_json({"x": 1})


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
