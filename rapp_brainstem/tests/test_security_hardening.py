"""Pinning tests for the LOCAL-ONLY security/robustness hardening pass.

Each test guards one audited fix so it can't silently regress. Hermetic: no network,
no real token, and no repo state files touched (the LAN secret is injected in-memory;
the single voice.zip round-trip creates + removes its own artifact).

    python3 -m pytest tests/test_security_hardening.py -v
"""
import json
import os

import pytest
from unittest import mock

import brainstem as bs


@pytest.fixture
def client():
    return bs.app.test_client()


def _parse_sse(text):
    """Parse an SSE body into a list of decoded JSON event dicts."""
    events = []
    for block in text.split("\n\n"):
        for line in block.split("\n"):
            if line.startswith("data:"):
                try:
                    events.append(json.loads(line[5:].strip()))
                except Exception:
                    pass
    return events


class _FakeExchangeResp:
    """Stand-in for the Copilot token-exchange response — its body carries a token."""
    def __init__(self, status=200):
        self.status_code = status
        self.text = ('{"token":"tid=LEAKED_COPILOT_TOKEN_XYZ;exp=9999999999;sku=free",'
                     '"expires_at":9999999999,"endpoints":{"api":"https://api.example"}}')

    def raise_for_status(self):
        pass

    def json(self):
        return json.loads(self.text)


# ── #1 CRITICAL: /debug/auth must not leak a token, and is loopback-only ───────

def test_debug_auth_never_returns_token_body(client, monkeypatch):
    monkeypatch.setattr(bs, "get_github_token", lambda: "ghu_faketoken1234567890")
    monkeypatch.setattr(bs, "_read_token_file", lambda: {"access_token": "ghu_x", "refresh_token": "r"})
    monkeypatch.setattr(bs, "_load_copilot_cache", lambda: None)
    monkeypatch.setattr(bs, "_exchange_github_for_copilot", lambda t: _FakeExchangeResp(200))

    r = client.get("/debug/auth")  # test_client default remote_addr is 127.0.0.1 (loopback)
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    data = r.get_json()
    # The live token from the exchange body must NEVER appear anywhere in the response.
    assert "LEAKED_COPILOT_TOKEN_XYZ" not in body
    assert "exchange_response" not in data          # the leaking key is gone
    assert data.get("exchange_http_status") == 200  # only status/booleans remain
    assert data.get("exchange_ok") is True


def test_debug_auth_forbidden_from_non_loopback(client, monkeypatch):
    # Guard against it even attempting an exchange for a remote caller.
    called = {"exchange": False}

    def _boom(_t):
        called["exchange"] = True
        return _FakeExchangeResp(200)

    monkeypatch.setattr(bs, "get_github_token", lambda: "ghu_faketoken1234567890")
    monkeypatch.setattr(bs, "_exchange_github_for_copilot", _boom)

    r = client.get("/debug/auth", environ_overrides={"REMOTE_ADDR": "10.0.0.42"})
    assert r.status_code == 403
    assert r.is_json and "error" in r.get_json()
    body = r.get_data(as_text=True)
    assert "LEAKED_COPILOT_TOKEN_XYZ" not in body
    assert called["exchange"] is False  # never even ran the exchange


# ── #2 HIGH: token/exchange body is scrubbed before logging ────────────────────

def test_scrub_secrets_redacts_json_token_and_bearer():
    body = ('{"token":"tid=SECRET_ABC;exp=1","expires_at":1,'
            '"endpoints":{"api":"https://ep.example"}}')
    out = bs._scrub_secrets(body)
    assert "SECRET_ABC" not in out
    assert "REDACTED" in out
    assert "https://ep.example" in out  # non-secret fields preserved

    raw = "Authorization: Bearer abc.def.ghijklmnop trailing"
    out2 = bs._scrub_secrets(raw)
    assert "abc.def.ghijklmnop" not in out2 and "REDACTED" in out2


def test_exchange_2xx_logs_status_only(monkeypatch, capsys):
    # A successful exchange must log only the status — never the (token-bearing) body.
    monkeypatch.setattr(bs.requests, "get", lambda *a, **k: _FakeExchangeResp(200))
    bs._exchange_github_for_copilot("ghu_faketoken1234567890")
    printed = capsys.readouterr().out
    assert "LEAKED_COPILOT_TOKEN_XYZ" not in printed
    assert "HTTP 200 (ok)" in printed


# ── #3 CRITICAL: LAN mutating routes require the secret; loopback is exempt ─────

MUTATING = [
    ("post", "/agents/import"),
    ("post", "/voice/import"),
    ("delete", "/agents/basic_agent.py"),
]


@pytest.mark.parametrize("method,path", MUTATING)
def test_mutating_route_loopback_exempt(client, method, path):
    # Loopback (same-machine UI) must reach the handler WITHOUT any secret. We send no
    # file / target the undeletable base class, so the handler short-circuits with a
    # 4xx that is NOT 403 — proving the gate let it through without side effects.
    r = getattr(client, method)(path)
    assert r.status_code != 403


@pytest.mark.parametrize("method,path", MUTATING)
def test_mutating_route_blocks_lan_without_secret(client, monkeypatch, method, path):
    monkeypatch.setattr(bs, "BRAINSTEM_SECRET", "unit-test-secret")
    r = getattr(client, method)(path, environ_overrides={"REMOTE_ADDR": "192.168.1.50"})
    assert r.status_code == 403
    assert r.is_json and "error" in r.get_json()


@pytest.mark.parametrize("method,path", MUTATING)
def test_mutating_route_allows_lan_with_secret(client, monkeypatch, method, path):
    monkeypatch.setattr(bs, "BRAINSTEM_SECRET", "unit-test-secret")
    r = getattr(client, method)(
        path,
        headers={"X-Brainstem-Secret": "unit-test-secret"},
        environ_overrides={"REMOTE_ADDR": "192.168.1.50"},
    )
    assert r.status_code != 403  # gate passed → handler ran (returns its own 4xx)


def test_mutating_route_rejects_wrong_secret(client, monkeypatch):
    monkeypatch.setattr(bs, "BRAINSTEM_SECRET", "unit-test-secret")
    r = client.post(
        "/agents/import",
        headers={"X-Brainstem-Secret": "WRONG"},
        environ_overrides={"REMOTE_ADDR": "192.168.1.50"},
    )
    assert r.status_code == 403


def test_basic_agent_survives_lan_delete_attempt_with_secret(client, monkeypatch):
    monkeypatch.setattr(bs, "BRAINSTEM_SECRET", "unit-test-secret")
    r = client.delete(
        "/agents/basic_agent.py",
        headers={"X-Brainstem-Secret": "unit-test-secret"},
        environ_overrides={"REMOTE_ADDR": "192.168.1.50"},
    )
    assert r.status_code == 400  # gate passed, handler refuses to delete the base class
    assert os.path.exists(os.path.join(bs._BASE_DIR, "agents", "basic_agent.py"))


# ── #3c CORS restricted to localhost origins ───────────────────────────────────

def test_cors_allows_localhost_blocks_other_origins(client):
    ok = client.get("/version", headers={"Origin": "http://localhost:7071"})
    assert ok.headers.get("Access-Control-Allow-Origin") == "http://localhost:7071"
    bad = client.get("/version", headers={"Origin": "http://evil.example.com"})
    assert bad.headers.get("Access-Control-Allow-Origin") is None


def test_foreign_origin_cannot_post_to_loopback(client, monkeypatch):
    called = {"chat": False}

    def fake_load_agents():
        called["chat"] = True
        return {}

    monkeypatch.setattr(bs, "load_agents", fake_load_agents)
    r = client.post(
        "/chat",
        data=json.dumps({"user_input": "run something"}),
        content_type="text/plain",
        headers={"Origin": "https://evil.example"},
        environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert r.status_code == 403 and r.is_json
    assert called["chat"] is False


def test_management_route_blocks_lan_without_secret(client, monkeypatch):
    monkeypatch.setattr(bs, "BRAINSTEM_SECRET", "unit-test-secret")
    r = client.post(
        "/voice/toggle",
        json={"enabled": True},
        environ_overrides={"REMOTE_ADDR": "192.168.1.50"},
    )
    assert r.status_code == 403


@pytest.mark.parametrize("path", [
    "/agents",
    "/agents/export/basic_agent.py",
    "/diagnostics",
    "/diagnostics/book.json",
    "/login/status",
])
def test_sensitive_reads_block_lan_without_secret(client, monkeypatch, path):
    monkeypatch.setattr(bs, "BRAINSTEM_SECRET", "unit-test-secret")
    r = client.get(path, environ_overrides={"REMOTE_ADDR": "192.168.1.50"})
    assert r.status_code == 403


def test_cross_site_sensitive_get_blocked_on_loopback(client, monkeypatch):
    monkeypatch.setattr(bs, "BRAINSTEM_SECRET", "unit-test-secret")
    r = client.get(
        "/agents",
        headers={"Sec-Fetch-Site": "cross-site"},
        environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert r.status_code == 403


# ── #4 MEDIUM: request size cap configured ─────────────────────────────────────

def test_max_content_length_configured():
    assert bs.app.config.get("MAX_CONTENT_LENGTH") == 16 * 1024 * 1024


# ── #6 MEDIUM: voice password via header, not query string ─────────────────────

def test_voice_config_uses_header_password_not_query(client, monkeypatch):
    import pyzipper
    voice_zip = os.path.join(bs._BASE_DIR, "voice.zip")
    assert not os.path.exists(voice_zip), "test would clobber a real voice.zip"
    monkeypatch.setattr(bs, "VOICE_ZIP_PW", None)  # deterministic: no env default
    try:
        with pyzipper.AESZipFile(voice_zip, "w",
                                 compression=pyzipper.ZIP_DEFLATED,
                                 encryption=pyzipper.WZ_AES) as zf:
            zf.setpassword(b"pw123")
            zf.writestr("voice.json", json.dumps({"greeting": "hi"}))

        # Correct password via the HEADER → config is returned.
        ok = client.get("/voice/config", headers={"X-Voice-Password": "pw123"})
        assert ok.status_code == 200 and ok.get_json() == {"greeting": "hi"}

        # Same password via the QUERY STRING (no header) → ignored → cannot decrypt.
        bad = client.get("/voice/config?password=pw123")
        assert bad.status_code != 200 or bad.get_json() != {"greeting": "hi"}
    finally:
        if os.path.exists(voice_zip):
            os.remove(voice_zip)


# ── #7 MEDIUM: streaming surfaces NO_COPILOT_ACCESS as a structured event ───────

def test_stream_surfaces_no_copilot_access_structured(client):
    def fake_stream(*a, **k):
        raise RuntimeError("NO_COPILOT_ACCESS:octo@example.com")
        yield  # pragma: no cover — makes this a generator, like the real one

    with mock.patch.object(bs, "load_soul", return_value="SOUL"), \
         mock.patch.object(bs, "load_agents", return_value={}), \
         mock.patch.object(bs, "call_copilot_stream", side_effect=fake_stream), \
         mock.patch.object(bs, "call_copilot",
                           side_effect=RuntimeError("NO_COPILOT_ACCESS:octo@example.com")):
        resp = client.post("/chat/stream", json={"user_input": "hi"})
        events = _parse_sse(resp.get_data(as_text=True))

    errs = [e for e in events if e.get("type") == "error"]
    assert errs, f"expected an error event, got: {events}"
    e = errs[-1]
    assert e.get("no_copilot_access") is True
    assert e.get("copilot_username") == "octo@example.com"
    assert e.get("error", "").startswith("NO_COPILOT_ACCESS:")


def test_stream_disconnect_deterministically_closes_inner_generator(client):
    state = {"closed": False}
    holder = {}

    def inner():
        try:
            for i in range(5):
                yield ("delta", "c%d " % i)
            yield ("done", {"message": {"role": "assistant", "content": "x"},
                            "model": "gpt-4o", "finish_reason": "stop"})
        finally:
            state["closed"] = True

    def fake_stream(*a, **k):
        g = inner()
        holder["gen"] = g  # a strong ref defeats refcount-GC auto-close of the inner gen
        return g

    with mock.patch.object(bs, "load_soul", return_value="SOUL"), \
         mock.patch.object(bs, "load_agents", return_value={}), \
         mock.patch.object(bs, "call_copilot_stream", side_effect=fake_stream):
        resp = client.post("/chat/stream", json={"user_input": "hi"})
        it = resp.response
        next(it)  # pull one delta → outer generator suspended mid inner-stream
        assert state["closed"] is False
        resp.close()  # simulate client disconnect
    # Even though `holder` keeps the inner generator alive, the outer generator's
    # explicit .close() in finally must have closed it (its finally ran).
    assert state["closed"] is True


# ── #8 LOW: bad request bodies yield JSON 400, never Werkzeug HTML ──────────────

def test_models_set_malformed_json_is_json_400(client):
    r = client.post("/models/set", data="{ not json", content_type="application/json")
    assert r.status_code == 400 and r.is_json and "error" in r.get_json()


def test_voice_config_save_non_object_json_is_json_400(client):
    r = client.post("/voice/config", json=[1, 2, 3])
    assert r.status_code == 400 and r.is_json and "error" in r.get_json()


def test_voice_toggle_empty_body_toggles_not_error(client):
    before = bs.VOICE_MODE
    try:
        r = client.post("/voice/toggle")  # no body at all
        assert r.status_code == 200 and r.is_json
        assert r.get_json()["voice_mode"] == (not before)
    finally:
        bs.VOICE_MODE = before
