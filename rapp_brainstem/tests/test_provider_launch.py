"""Executable launcher, registration, and cross-platform kernel-byte checks."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from kernel_compat import KERNEL_SHA256


ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
CHECK_RUNNER = """
import pathlib, requests, runpy, socket, subprocess, sys
def refuse(*args, **kwargs):
    raise AssertionError('EXTERNAL_IO_FORBIDDEN')
requests.sessions.Session.request = refuse
socket.socket.connect = refuse
socket.getaddrinfo = refuse
subprocess.Popen = refuse
path = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(path.parent))
sys.argv = [str(path), '--check']
runpy.run_path(str(path), run_name='__main__')
"""


def environment(home, **extra):
    result = {
        "PATH": os.environ.get("PATH", ""), "HOME": str(home), "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"), "GH_CONFIG_DIR": str(home / "gh"),
        "PYTHONDONTWRITEBYTECODE": "1", "GITHUB_MODEL": "gpt-4o",
        "BRAINSTEM_LAN_MODE": "false",
    }
    for key in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP"):
        if key in os.environ:
            result[key] = os.environ[key]
    result.update(extra)
    return result


@pytest.fixture
def package(tmp_path):
    package = tmp_path / "package"
    for relative in (
        "brainstem.py", "launch.py", "kernel_compat.py", "provider_host.py",
        "runtime_profile.json", "VERSION",
        "provider_plugins/__init__.py", "provider_plugins/base.py", "provider_plugins/plugins.json",
    ):
        destination = package / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
    (package / ".env").write_text("", encoding="utf-8")
    return package


def check(package, home, **overrides):
    return subprocess.run(
        [sys.executable, "-I", "-c", CHECK_RUNNER, str(package / "launch.py")],
        cwd=package, env=environment(home, **overrides), capture_output=True, text=True,
    )


def test_check_mode_does_not_start_server_auth_or_agents(package, tmp_path):
    result = check(package, tmp_path, BRAINSTEM_PROVIDER_PLUGINS="none")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report == {"kernel_sha256": KERNEL_SHA256, "provider_api": 1, "plugins": []}
    for name in (".brainstem_model", ".copilot_token", ".copilot_session", ".brainstem_book.json"):
        assert not (package / name).exists()
    assert hashlib.sha256((package / "brainstem.py").read_bytes()).hexdigest() == KERNEL_SHA256


def test_profile_selects_only_its_explicit_builtin(package, tmp_path):
    module = package / "provider_plugins" / "fixture.py"
    module.write_text(
        "from provider_plugins.base import *\n"
        "class Fixture(ProviderPlugin):\n"
        "    spec = PluginSpec('fixture', 1, CAPABILITIES, '1.0.0')\n"
        "    def supports_model(self, model): return False\n"
        "    def prepare_request(self, body, model, context): raise AssertionError('unexpected inference')\n"
        "    def translate_response(self, payload, model, context): raise AssertionError('unexpected inference')\n"
        "    def translate_stream(self, lines, model, context): return iter(())\n",
        encoding="utf-8",
    )
    (package / "provider_plugins" / "unused.py").write_text(
        "raise AssertionError('unselected plugin was imported')\n", encoding="utf-8"
    )
    manifest = json.loads((package / "provider_plugins" / "plugins.json").read_text())
    declaration = manifest["plugins"][0]
    declaration.update(id="fixture", entrypoint="provider_plugins.fixture:Fixture")
    manifest["plugins"].append({**declaration, "id": "unused", "entrypoint": "provider_plugins.unused:Unused"})
    (package / "provider_plugins" / "plugins.json").write_text(json.dumps(manifest))
    profile = json.loads((package / "runtime_profile.json").read_text())
    profile["providers"] = ["fixture"]
    (package / "runtime_profile.json").write_text(json.dumps(profile))
    result = check(package, tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["plugins"] == [{"id": "fixture", "version": "1.0.0"}]


def test_same_version_with_changed_kernel_is_refused(package, tmp_path):
    kernel = package / "brainstem.py"
    kernel.write_bytes(kernel.read_bytes() + b"\n# invalid fixture mutation\n")
    result = check(package, tmp_path, BRAINSTEM_PROVIDER_PLUGINS="none")
    assert result.returncode == 1
    assert "Kernel compatibility check failed" in result.stderr
    assert "EXTERNAL_IO_FORBIDDEN" not in result.stderr


def test_unknown_runtime_profile_does_not_fall_back(package, tmp_path):
    profile = json.loads((package / "runtime_profile.json").read_text())
    profile["provider_api"] = 2
    (package / "runtime_profile.json").write_text(json.dumps(profile))
    result = check(package, tmp_path, BRAINSTEM_PROVIDER_PLUGINS="none")
    assert result.returncode == 1
    assert "Incompatible Brainstem runtime profile" in result.stderr


def test_kernel_checkout_is_identical_even_with_windows_line_ending_settings(tmp_path):
    repo = tmp_path / "repository"
    repo.mkdir()
    env = environment(tmp_path, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args], env=env, capture_output=True, text=True, check=True
        )
    git("init", "-q", "--template=")
    git("config", "user.name", "Kernel Fixture")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "core.autocrlf", "true")
    (repo / "rapp_brainstem").mkdir()
    shutil.copyfile(ROOT / "brainstem.py", repo / "rapp_brainstem" / "brainstem.py")
    shutil.copyfile(REPO / ".gitattributes", repo / ".gitattributes")
    git("add", ".gitattributes", "rapp_brainstem/brainstem.py")
    git("commit", "--no-gpg-sign", "-qm", "fixture")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    git("checkout-index", "--all", "--prefix=" + checkout.as_posix() + "/")
    assert hashlib.sha256((checkout / "rapp_brainstem" / "brainstem.py").read_bytes()).hexdigest() == KERNEL_SHA256


@pytest.fixture
def unix_launcher(tmp_path):
    if os.name == "nt":
        pytest.skip("Unix wrapper execution; native Windows wrappers are covered in hosted preflight")
    home = tmp_path / "home"
    source = home / ".brainstem" / "src" / "rapp_brainstem"
    python = home / ".brainstem" / "venv" / "bin" / "python"
    source.mkdir(parents=True)
    python.parent.mkdir(parents=True)
    python.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$TRACE\"\n"
        "if [ \"$1\" = launch.py ] && [ \"${2:-}\" = --check ]; then\n"
        "    exit \"${FAIL_CHECK:-0}\"\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    (source / "brainstem.py").write_text("# legacy fixture\n")
    (source / ".env.example").write_text("")
    trace = tmp_path / "calls.txt"
    env = environment(home, TRACE=str(trace))
    return home, source, trace, env


@pytest.mark.parametrize("generated", [False, True])
@pytest.mark.parametrize("mode", ["legacy", "provider", "broken", "refused"])
def test_unix_startup_dispatch_and_fail_closed_validation(unix_launcher, generated, mode):
    home, source, trace, env = unix_launcher
    if mode != "legacy":
        (source / "runtime_profile.json").write_text("{}")
    if mode in ("provider", "refused"):
        (source / "launch.py").write_text("# provider fixture\n")
    if mode == "refused":
        env["FAIL_CHECK"] = "7"
    if generated:
        installer = (REPO / "install.sh").read_text(encoding="utf-8").replace("\r\n", "\n")
        helper = "install_cli() {" + installer.split("install_cli() {", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
        env.update(BRAINSTEM_HOME=str(home / ".brainstem"), BRAINSTEM_BIN=str(home / ".local" / "bin"))
        setup = subprocess.run(["bash", "-c", helper + "\ninstall_cli\n"], env=env, capture_output=True, text=True)
        assert setup.returncode == 0, setup.stderr
        launcher = home / ".local" / "bin" / "brainstem"
    else:
        launcher = source / "start.sh"
        shutil.copyfile(ROOT / "start.sh", launcher)
    result = subprocess.run(["bash", str(launcher)], env=env, capture_output=True, text=True)
    calls = trace.read_text().splitlines() if trace.exists() else []
    if mode == "legacy":
        assert result.returncode == 0
        assert calls[-1] == "brainstem.py"
    elif mode == "provider":
        assert result.returncode == 0, result.stderr
        assert calls[-2:] == ["launch.py --check", "launch.py"]
    else:
        assert result.returncode != 0
        assert "brainstem.py" not in calls
        assert "launch.py" not in calls
