"""Pinning tests for the lost-sign-in fix (fix/state-survives-upgrade).

Users reported that they could not stay signed in, and stopped using the
application because of it. One cause was not in the auth logic — that side is
careful, and says in as many words "Token exchange failed — NEVER delete the
token file". The cause was WHERE the credential lived.

Every piece of per-install state was written next to brainstem.py, inside
$BRAINSTEM_HOME/src. The installer's repair path for a checkout whose .git
directory is missing does `rm -rf "$BRAINSTEM_HOME/src"` followed by a fresh
clone. It preserves soul.md, .env, custom agents and .brainstem_data across that;
it never preserved the rest. A repaired install therefore signed the person out,
regenerated the LAN secret, and forgot the model they had picked.

These tests hold the fix down:
  * state resolves BESIDE the checkout, never inside it
  * a file left in the old in-tree location is migrated forward, so nobody has to
    sign in again in order to receive the fix
  * migration COPIES rather than moves, so an older brainstem still running
    against the same install keeps working
  * and the whole point: it is still there after `rm -rf src`
"""
import os
import shutil
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
BRAINSTEM_DIR = os.path.dirname(HERE)
ROOT = os.path.dirname(BRAINSTEM_DIR)
BRAINSTEM = os.path.join(BRAINSTEM_DIR, "brainstem.py")

# The state helpers are module-level and run at import time, so lift just those
# two functions out rather than importing the whole server.
_SRC = open(BRAINSTEM, encoding="utf-8").read()
_HELPER = _SRC[_SRC.index("_STATE_NAMES = ("):_SRC.index("_model_file = _state_path")]

STATE_NAMES = [".copilot_token", ".copilot_session", ".brainstem_secret",
               ".brainstem_model", ".brainstem_book.json"]


def helpers_for(install_dir, state_dir):
    """Load _state_dir/_state_path bound to a throwaway install."""
    ns = {"os": os, "shutil": shutil, "tempfile": tempfile,
          "__file__": os.path.join(install_dir, "brainstem.py")}
    os.environ["BRAINSTEM_STATE_DIR"] = state_dir
    exec(compile(_HELPER, "state_helpers", "exec"), ns)
    return ns["_state_path"]


class StateSurvivesUpgrade(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="brainstem-home-")
        self.install = os.path.join(self.home, "src", "rapp_brainstem")
        self.state = os.path.join(self.home, "state")
        os.makedirs(self.install, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.home, True)
        self.addCleanup(os.environ.pop, "BRAINSTEM_STATE_DIR", None)

    def test_state_lives_outside_the_checkout(self):
        state_path = helpers_for(self.install, self.state)
        for name in STATE_NAMES:
            p = state_path(name)
            self.assertNotIn(os.path.join("src", "rapp_brainstem"), p,
                             f"{name} still resolves inside the tree the installer replaces")

    def test_an_existing_sign_in_is_migrated_forward(self):
        legacy = os.path.join(self.install, ".copilot_token")
        with open(legacy, "w", encoding="utf-8") as f:
            f.write('{"token": "ghu_pretend", "expires_at": 9999999999}')
        state_path = helpers_for(self.install, self.state)
        moved = state_path(".copilot_token")
        self.assertTrue(os.path.exists(moved), "an existing sign-in was not carried forward")
        with open(moved, encoding="utf-8") as f:
            self.assertIn("ghu_pretend", f.read())
        # copied, not moved: an older brainstem against the same install still reads it
        self.assertTrue(os.path.exists(legacy),
                        "migration MOVED the file; an older brainstem would lose its sign-in")

    def test_intentional_deletion_does_not_resurrect_the_legacy_token(self):
        legacy = os.path.join(self.install, ".copilot_token")
        with open(legacy, "w", encoding="utf-8") as f:
            f.write('{"access_token": "ghu_stale"}')
        state_path = helpers_for(self.install, self.state)
        migrated = state_path(".copilot_token")
        os.remove(migrated)

        state_path = helpers_for(self.install, self.state)
        self.assertFalse(
            os.path.exists(state_path(".copilot_token")),
            "a deliberately removed token was resurrected from the legacy checkout",
        )

    def test_failed_copy_never_leaves_a_partial_destination_or_marker(self):
        legacy = os.path.join(self.install, ".copilot_token")
        with open(legacy, "w", encoding="utf-8") as f:
            f.write('{"access_token": "ghu_complete_source"}')
        marker = os.path.join(self.state, ".legacy-state-migrated-v1")

        with mock.patch.object(shutil, "copy2", side_effect=OSError("disk full")):
            state_path = helpers_for(self.install, self.state)
            resolved = state_path(".copilot_token")

        self.assertEqual(resolved, legacy)
        self.assertFalse(os.path.exists(os.path.join(self.state, ".copilot_token")))
        self.assertFalse(os.path.exists(marker))

    def test_the_sign_in_survives_the_installer_wiping_src(self):
        with open(os.path.join(self.install, ".copilot_token"), "w", encoding="utf-8") as f:
            f.write('{"token": "ghu_pretend"}')
        state_path = helpers_for(self.install, self.state)
        token = state_path(".copilot_token")
        # this is the installer's update path, verbatim: rm -rf "$BRAINSTEM_HOME/src"
        shutil.rmtree(os.path.join(self.home, "src"))
        self.assertTrue(os.path.exists(token),
                        "the sign-in did not survive an upgrade — the reported bug is back")

    def test_a_fresh_install_needs_no_migration(self):
        state_path = helpers_for(self.install, self.state)
        p = state_path(".copilot_token")
        self.assertFalse(os.path.exists(p), "invented a credential where there was none")

    def test_an_unwritable_home_still_returns_a_usable_path(self):
        # Losing the process is worse than losing the upgrade-survival property.
        os.environ["BRAINSTEM_STATE_DIR"] = "/proc/nonexistent/cannot-create"
        ns = {"os": os, "shutil": shutil, "tempfile": tempfile,
              "__file__": os.path.join(self.install, "brainstem.py")}
        exec(compile(_HELPER, "state_helpers", "exec"), ns)
        p = ns["_state_path"](".copilot_token")
        self.assertTrue(isinstance(p, str) and p, "no usable path when the state dir cannot be made")


class InstallerMigratesBeforeReplacingTheCheckout(unittest.TestCase):
    @staticmethod
    def _source(name):
        with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
            return handle.read().replace("\r\n", "\n")

    def test_shell_installer_copies_every_state_file_before_rm_rf(self):
        source = self._source("install.sh")
        helper_start = source.index("migrate_legacy_state() {")
        helper_end = source.index("\ninstall_brainstem() {", helper_start)
        helper = source[helper_start:helper_end]
        install_end = source.index("\nsetup_venv() {")
        install = source[helper_end:install_end]

        for name in STATE_NAMES:
            self.assertIn(name, helper)
        self.assertLess(
            helper.index('mkdir -p "$state_dir"'),
            helper.index('[ -f "$marker" ]'),
            "fresh installs can return before creating the state directory",
        )
        self.assertIn("copy_state_file_atomically", helper)
        self.assertIn("write_state_migration_marker", helper)
        self.assertLess(
            install.index("migrate_legacy_state"),
            install.index('rm -rf "$BRAINSTEM_HOME/src"'),
            "shell installer deletes the checkout before preserving legacy state",
        )

    def test_powershell_installer_copies_every_state_file_before_remove_item(self):
        source = self._source("install.ps1")
        helper_start = source.index("function Migrate-LegacyState {")
        helper_end = source.index("\nfunction Install-Brainstem {", helper_start)
        helper = source[helper_start:helper_end]
        install_end = source.index("\nfunction Run-PipInstall {")
        install = source[helper_end:install_end]

        for name in STATE_NAMES:
            self.assertIn(name, helper)
        self.assertLess(
            helper.index("New-Item -ItemType Directory"),
            helper.index("Test-Path -LiteralPath $marker"),
            "fresh Windows installs can return before creating the state directory",
        )
        self.assertIn("Copy-StateFileAtomically", helper)
        self.assertIn("Write-StateMigrationMarker", helper)
        self.assertLess(
            install.index("Migrate-LegacyState"),
            install.index('Remove-Item -Recurse -Force "$BRAINSTEM_HOME\\src"'),
            "PowerShell installer deletes the checkout before preserving legacy state",
        )

    def test_installers_keep_rollback_compatibility(self):
        shell = self._source("install.sh")
        powershell = self._source("install.ps1")
        self.assertIn("prepare_legacy_runtime_state", shell)
        self.assertIn("Prepare-LegacyRuntimeState", powershell)
        self.assertIn("sync_live_legacy_state", shell)
        self.assertIn("Sync-LiveLegacyState", powershell)
        self.assertIn(".legacy-state-migrated-v1", shell)
        self.assertIn(".legacy-state-migrated-v1", powershell)

    def test_installers_stop_the_old_writer_before_the_final_state_snapshot(self):
        shell = self._source("install.sh")
        shell_launch = shell[shell.index("launch_brainstem() {"):shell.index("\nmain() {")]
        self.assertLess(
            shell_launch.index("stop_existing_brainstem"),
            shell_launch.index("sync_live_legacy_state"),
        )
        self.assertLess(
            shell_launch.index("sync_live_legacy_state"),
            shell_launch.index("local token_file="),
        )
        self.assertIn("! installed_runtime_uses_external_state", shell_launch)

        powershell = self._source("install.ps1")
        ps_launch = powershell[
            powershell.index("function Launch-Brainstem {"):
            powershell.index("\nfunction Main {")
        ]
        self.assertLess(
            ps_launch.index("Stop-ExistingBrainstem"),
            ps_launch.index("Sync-LiveLegacyState"),
        )
        self.assertLess(
            ps_launch.index("Sync-LiveLegacyState"),
            ps_launch.index("$tokenFile ="),
        )
        self.assertIn("Test-InstalledRuntimeUsesExternalState", ps_launch)

    def test_both_installers_cap_device_flow_slow_down(self):
        shell = self._source("install.sh")
        powershell = self._source("install.ps1")
        self.assertIn('[ "$interval" -le 30 ] || interval=30', shell)
        self.assertIn("[Math]::Min(30", powershell)

    def test_windows_accepts_plaintext_tokens_and_keeps_a_403_sign_in(self):
        source = self._source("install.ps1")
        self.assertIn("$savedToken = $rawToken", source)
        post_start = source.index("if ($pollResp.access_token)")
        post_end = source.index("$error_code = $pollResp.error", post_start)
        post_login = source[post_start:post_end]
        forbidden_start = post_login.index("elseif ($statusCode -eq 403)")
        forbidden_end = post_login.index("elseif ($statusCode -ne 0)", forbidden_start)
        self.assertNotIn("Remove-Item", post_login[forbidden_start:forbidden_end])
        self.assertIn("Write-JsonFileAtomically", post_login)


if __name__ == "__main__":
    unittest.main()
