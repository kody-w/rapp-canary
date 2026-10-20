"""The terminal chat fallback (tui.py) that install.sh launches only when no GUI
browser can open. Hermetic: textual is stubbed or blocked, no server is started.

    python3 -m pytest tests/test_tui.py -v
"""
import os
import subprocess
import sys

BRAINSTEM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Minimal stand-ins for the textual names tui.py imports, so its module-level code
# runs without the optional dependency installed.
_STUB_TEXTUAL = """
import sys, types
for name in ("textual", "textual.app", "textual.containers", "textual.widgets"):
    sys.modules[name] = types.ModuleType(name)
sys.modules["textual.app"].App = type("App", (), {})
sys.modules["textual.app"].ComposeResult = object
for n in ("Horizontal", "Vertical"):
    setattr(sys.modules["textual.containers"], n, type(n, (), {}))
for n in ("Button", "Footer", "Header", "Input", "RichLog", "Static"):
    setattr(sys.modules["textual.widgets"], n, type(n, (), {}))
"""


def _run(code, **env):
    return subprocess.run(
        [sys.executable, "-c", code], cwd=BRAINSTEM_DIR,
        env=dict(os.environ, **env), capture_output=True, text=True, timeout=60,
    )


def test_missing_textual_exits_with_an_install_hint_instead_of_a_traceback():
    out = _run("import sys; sys.modules['textual'] = None; import tui")
    assert out.returncode == 1
    assert "pip install textual" in out.stderr
    assert "Traceback" not in out.stderr


def test_tui_talks_to_the_port_the_server_binds():
    code = _STUB_TEXTUAL + "import tui; print(tui.PORT, tui.BASE_URL)"
    ok = _run(code, PORT="7093")
    assert ok.returncode == 0, ok.stderr
    assert ok.stdout.split() == ["7093", "http://127.0.0.1:7093"]
    bad = _run(code, PORT="not-a-port")
    assert bad.returncode == 0, bad.stderr
    assert bad.stdout.split()[0] == "7071"
