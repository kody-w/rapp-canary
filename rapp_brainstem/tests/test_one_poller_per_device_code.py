"""Cross-process tests for the sign-in poller lock (fix/state-survives-upgrade).

Several brainstems can share one install directory. On the machine where this
was diagnosed, two LaunchAgents (com.brainstem.server and com.rapp.crispy-twin)
both ran brainstem.py with the same working directory. They therefore shared
.copilot_pending and polled the SAME device code. GitHub answered slow_down to
all of them, each ratcheted its own interval, and the code expired before it
could be redeemed - twice, in the live log. The user sees "signing in is broken".

These spawn REAL subprocesses. An in-process test would prove nothing here: the
whole failure is between processes, and the lock is only meaningful if the OS
enforces it across them - including releasing it when a holder is killed, which
is what stops a dead brainstem from wedging sign-in forever.
"""
import os
import errno
import subprocess
import sys
import tempfile
import shutil
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
sys.path.insert(0, PARENT)

import brainstem  # noqa: E402

CLAIM = """
import os, sys, time
sys.path.insert(0, %r)
os.environ["BRAINSTEM_STATE_DIR"] = sys.argv[1]
import brainstem as b
got = b._claim_the_poller()
if got and len(sys.argv) > 2:
    open(os.path.join(sys.argv[1], "held"), "w").write("yes")
    time.sleep(float(sys.argv[2]))
sys.exit(0 if got else 3)
""" % PARENT


@unittest.skipIf(sys.platform == "win32", "advisory flock is POSIX; Windows is best-effort")
class OnlyOneBrainstemPolls(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="poller-")
        self.addCleanup(shutil.rmtree, self.state, True)

    def _claim(self, hold=None):
        argv = [sys.executable, "-c", CLAIM, self.state] + ([str(hold)] if hold else [])
        if hold:
            return subprocess.Popen(argv)
        return subprocess.run(argv, capture_output=True).returncode == 0

    def _await_held(self, proc, timeout=20):
        flag = os.path.join(self.state, "held")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(flag):
                return True
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        return False

    def test_a_second_brainstem_declines_the_same_device_code(self):
        holder = self._claim(hold=30)
        self.addCleanup(holder.kill)
        self.assertTrue(self._await_held(holder), "the holder never took the lock")
        self.assertFalse(self._claim(), "two brainstems polled the same device code")

    def test_a_killed_brainstem_does_not_wedge_sign_in(self):
        # The reason this is an advisory flock and not a pid file: if it could
        # outlive its holder, one crash would make sign-in permanently impossible.
        holder = self._claim(hold=60)
        self.addCleanup(holder.kill)
        self.assertTrue(self._await_held(holder), "the holder never took the lock")
        holder.kill()
        holder.wait(timeout=10)
        os.remove(os.path.join(self.state, "held"))
        self.assertTrue(self._claim(), "a killed brainstem left sign-in wedged")

    def test_the_lock_is_released_when_the_holder_finishes(self):
        holder = self._claim(hold=1)
        self.assertTrue(self._await_held(holder), "the holder never took the lock")
        holder.wait(timeout=20)
        self.assertTrue(self._claim(), "the lock outlived its holder")

    def test_an_unsupported_flock_does_not_silently_block_sign_in(self):
        brainstem._poll_lock_fh = None
        with mock.patch.dict(os.environ, {"BRAINSTEM_STATE_DIR": self.state}), \
                mock.patch.object(
                    brainstem.fcntl,
                    "flock",
                    side_effect=OSError(errno.ENOTSUP, "flock unsupported"),
                ):
            self.assertTrue(
                brainstem._claim_the_poller(),
                "an unavailable lock was mistaken for sibling contention",
            )
        self.assertIsNone(brainstem._poll_lock_fh)

    def test_real_lock_contention_still_defers_to_the_holder(self):
        brainstem._poll_lock_fh = None
        with mock.patch.dict(os.environ, {"BRAINSTEM_STATE_DIR": self.state}), \
                mock.patch.object(
                    brainstem.fcntl,
                    "flock",
                    side_effect=OSError(errno.EAGAIN, "lock held"),
                ):
            self.assertFalse(brainstem._claim_the_poller())
        self.assertIsNone(brainstem._poll_lock_fh)


class ADeferredBrainstemTakesOver(unittest.TestCase):
    def setUp(self):
            brainstem._pending_login = {
                "device_code": "device",
                "user_code": "CODE",
                "expires_at": time.time() + 60,
                "started_at": time.time(),
            }
            brainstem._login_result = {}
            brainstem._poll_lock_fh = None
            self.addCleanup(setattr, brainstem, "_pending_login", {})
            self.addCleanup(setattr, brainstem, "_login_result", {})
            self.addCleanup(setattr, brainstem, "_poll_lock_fh", None)

    def test_a_deferred_sibling_retries_and_takes_over_after_holder_dies(self):
            with mock.patch.object(brainstem, "_claim_the_poller", side_effect=[False, True]), \
                    mock.patch.object(brainstem, "_sibling_completed_the_login", return_value=False), \
                    mock.patch.object(brainstem, "_bg_poll_forever") as poll, \
                    mock.patch.object(brainstem, "_release_the_poller") as release, \
                    mock.patch.object(brainstem.time, "sleep"):
                brainstem._bg_poll_loop()

            poll.assert_called_once_with()
            release.assert_called_once_with()

    def test_a_deferred_sibling_observes_the_holders_success(self):
            with mock.patch.object(brainstem, "_claim_the_poller", return_value=False), \
                    mock.patch.object(brainstem, "_sibling_completed_the_login", return_value=True), \
                    mock.patch.object(brainstem.time, "sleep") as sleep:
                brainstem._bg_poll_loop()

            self.assertEqual(brainstem._login_result.get("status"), "ok")
            sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
