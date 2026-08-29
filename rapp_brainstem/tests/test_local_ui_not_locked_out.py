"""Pinning tests for the local-UI lockout (fix/state-survives-upgrade).

The brainstem was refusing its own local UI. On this machine the event log had
`auth.secret_denied` as the single most frequent event - 401 of them, every one
from 127.0.0.1, most on /login/status, the route the browser polls to learn a
device sign-in finished. So a user could complete the GitHub sign-in and the page
would never find out: it kept polling, kept getting 403, and kept saying signed
out. That is the reported "can't stay signed in", and no token was even involved.

The bundled UI uses relative URLs, so it never needs cross-origin loopback
authority. That is important: another process can own [::1]:7071 while the
brainstem owns 127.0.0.1:7071. Treating those origins as equivalent would let
unrelated local web content import executable agents.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import brainstem  # noqa: E402

HOST = "127.0.0.1:7071"
INDEX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "index.html")


def foreign(headers=None, method="GET", host=HOST, path="/login/status"):
    """Run the real guard inside a real request context."""
    with brainstem.app.test_request_context(
            path, method=method, headers=headers or {}, base_url=f"http://{host}"):
        return brainstem._is_foreign_browser_request()


class TheLocalUiGetsIn(unittest.TestCase):
    def test_the_bundled_ui_uses_same_origin_requests(self):
        with open(INDEX, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn(
            "const API = (location.protocol === 'http:' || location.protocol === 'https:') ? ''",
            source,
        )

    def test_a_plain_same_origin_request_is_unaffected(self):
        self.assertFalse(foreign({"Origin": f"http://{HOST}"}))
        self.assertFalse(foreign({"Origin": "http://localhost:7071"}, host="localhost:7071"))
        self.assertFalse(foreign({"Sec-Fetch-Site": "same-origin"}))
        self.assertFalse(foreign())

    def test_every_other_loopback_origin_is_refused(self):
        for origin in (
                "http://localhost:7071",
                "http://[::1]:7071",
                "http://127.0.0.2:7071",
                "http://localhost:8000",
                "http://127.0.0.1:3000",
                "http://[::1]:5173",
                "http://localhost:8888",
        ):
            for method in ("GET", "POST"):
                self.assertTrue(foreign({"Origin": origin}, method=method),
                                f"{origin} regained access on {method}")

    def test_a_different_scheme_on_the_same_port_is_not_this_server(self):
        self.assertTrue(foreign({"Origin": "https://localhost:7071"}, method="POST"))

    def test_userinfo_cannot_smuggle_a_loopback_host(self):
        # These parse to hostname 127.0.0.1 but no browser emits them.
        for origin in ("http://evil.example@127.0.0.1:7071",
                       "http://user:pass@127.0.0.1:7071",
                       "http://evil.example:80@127.0.0.1:7071"):
            self.assertTrue(foreign({"Origin": origin}, method="POST"),
                            f"{origin} was smuggled through as this machine")

class AStrangerStillDoesNot(unittest.TestCase):
    """If any of these ever passes, the guard has been broken open."""

    def test_a_remote_page_is_refused_on_every_method(self):
        for method in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"):
            self.assertTrue(foreign({"Origin": "https://evil.example"}, method=method),
                            f"a remote origin got through on {method}")

    def test_a_cross_site_request_with_no_origin_is_refused_on_every_method(self):
        # Including GET: an unreadable reply is not a harmless one. /agents is a
        # sensitive GET and test_security_hardening.py pins that it stays blocked.
        for method in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"):
            self.assertTrue(foreign({"Sec-Fetch-Site": "cross-site"}, method=method),
                            f"a cross-site {method} got through")

    def test_an_origin_that_merely_mentions_loopback_is_refused(self):
        # Every one of these parses to a NON-loopback host. They are here because
        # each is a plausible way to try to look local.
        for origin in ("http://127.0.0.1@evil.example",
                       "http://evil.example#@127.0.0.1",
                       "http://evil.example/127.0.0.1",
                       "http://127.0.0.1.evil.example",
                       "http://localhost.evil.example",
                       "http://evil.example?x=http://127.0.0.1",
                       "https://xn--localhost-.evil.example"):
            self.assertTrue(foreign({"Origin": origin}, method="POST"),
                            f"{origin} was treated as this machine")

    def test_null_origin_is_not_this_machine(self):
        # file:// pages and sandboxed iframes both send this.
        self.assertTrue(foreign({"Origin": "null"}, method="POST"))

    def test_a_configured_host_name_does_not_become_a_trusted_origin(self):
        # _ALLOWED_HOSTS says which names the server ANSWERS to; it must not also
        # mean "pages from there may drive it".
        brainstem._ALLOWED_HOSTS.add("brainstem.lan")
        try:
            self.assertTrue(foreign({"Origin": "http://brainstem.lan"}, method="POST"))
        finally:
            brainstem._ALLOWED_HOSTS.discard("brainstem.lan")


class TheSignInPollCanFinish(unittest.TestCase):
    def test_the_login_interval_never_outlives_the_device_code(self):
        # It used to grow by 5s forever and was persisted across restarts, so the
        # sleeps consumed the code's ~900s life and the sign-in expired unredeemed.
        self.assertLessEqual(brainstem._LOGIN_MAX_INTERVAL, 60)
        interval = 5
        for _ in range(200):
            interval = max(1, min(interval + 5, brainstem._LOGIN_MAX_INTERVAL))
        self.assertLessEqual(interval, brainstem._LOGIN_MAX_INTERVAL)
        self.assertGreaterEqual(interval, 1)


class ATransientRejectionHeals(unittest.TestCase):
    """A 403 is routinely transient - rate limit, corporate proxy, an entitlement
    still landing. The flag that remembers "GitHub rejected this credential"
    recorded a timestamp from the start and nothing ever read it, so one transient
    rejection marked a good credential dead until the process restarted."""

    def setUp(self):
        self.addCleanup(brainstem._clear_invalid_github_credential)

    def test_a_rejected_credential_is_retested_after_the_ttl(self):
        token = "ghu_transiently_rejected"
        brainstem._set_invalid_github_credential(token, 403)
        self.assertTrue(brainstem._github_credential_is_invalid(token),
                        "the flag did not take effect at all")

        brainstem._invalid_github_credential["at"] = (
            time.time() - brainstem._INVALID_CREDENTIAL_TTL - 1)
        self.assertFalse(brainstem._github_credential_is_invalid(token),
                         "a transient rejection was still permanent")

    def test_the_ttl_is_long_enough_to_stop_a_retry_storm(self):
        self.assertGreaterEqual(brainstem._INVALID_CREDENTIAL_TTL, 60)

    def test_a_different_credential_is_never_tarred_by_the_flag(self):
        brainstem._set_invalid_github_credential("ghu_the_bad_one", 401)
        self.assertFalse(brainstem._github_credential_is_invalid("ghu_a_fresh_one"))


if __name__ == "__main__":
    unittest.main()
