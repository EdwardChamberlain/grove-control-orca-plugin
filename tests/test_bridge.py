import json
import re
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

from grove_control_orca_plugin.bridge import RemoteExchange, navigation_html
from grove_control_orca_plugin.models import AuthenticationRequired, ProtocolError


class FakeWindow:
    def __init__(self, host, callbacks):
        self.host = host
        self.callbacks = callbacks
        self.open = True
        self.posts = []

    def post(self, data):
        self.posts.append(data)
        if self.host.print_mode and data.get("type") == "initialize" and data.get("mode") == "connect":
            if self.host.authenticate:
                self.send("authenticated", access_token="browser-token")
        else:
            self.host.on_post(self, data)

    def close(self):
        self.open = False
        self.callbacks["on_close"]()

    def is_open(self):
        return self.open

    def send(self, kind, **data):
        self.callbacks["on_message"](
            {
                "type": kind,
                "protocol_version": 1,
                "request_id": self.host.request_id,
                **({"access_token": "browser-token"} if self.host.print_mode else {}),
                **data,
            }
        )


class FakeHost:
    WINDOW_MODELESS = 0
    WINDOW_MODAL = 1

    def __init__(self, on_post=lambda *_: None, ready=True, authenticate=True):
        self.on_post = on_post
        self.ready = ready
        self.authenticate = authenticate
        self.created = []

    def create_window(self, **callbacks):
        self.created.append(callbacks)
        url_match = re.search(r"replace\((.*)\);", callbacks["html"]) or re.search(
            r"new URL\((.*?)\);", callbacks["html"]
        )
        self.url = json.loads(url_match.group(1))
        self.print_mode = parse_qs(urlsplit(self.url).query)["mode"][0] == "print"
        self.request_id = parse_qs(urlsplit(self.url).query)["request_id"][0]
        self.window = FakeWindow(self, callbacks)
        if self.ready:
            # Exercise the race where ready arrives before create_window returns a handle.
            self.window.send("ready")
        return self.window


def make_exchange(host, url, mode, initialization, **kwargs):
    if mode == "print":
        kwargs.setdefault("verify_print_session", lambda token: None)
    return RemoteExchange(host, url, mode, initialization, **kwargs)


class BridgeTests(unittest.TestCase):
    def test_print_requires_an_account_verifier(self):
        with self.assertRaisesRegex(ValueError, "verification"):
            RemoteExchange(FakeHost(), "https://grove.example", "print", {})

    def test_browser_session_is_verified_before_file_initialization(self):
        verified = []

        def verify(token):
            self.assertEqual([p["mode"] for p in host.window.posts], ["connect"])
            verified.append(token)

        host = FakeHost(lambda window, _: window.send("submitted", queue_item_ids=[1]))
        result = make_exchange(
            host, "https://grove.example/proxy", "print", {"library_file_id": 42}, verify_print_session=verify
        ).run()
        self.assertEqual(verified, ["browser-token"])
        self.assertEqual(urlsplit(host.url).path, "/proxy/integrations/orca")
        self.assertNotIn("access_token", result)

    def test_different_browser_account_never_receives_file_initialization(self):
        def reject(token):
            raise AuthenticationRequired("Use the same account")

        host = FakeHost()
        exchange = make_exchange(host, "https://grove.example", "print", {}, verify_print_session=reject)
        with self.assertRaisesRegex(AuthenticationRequired, "same account"):
            exchange.run()
        self.assertEqual([p["mode"] for p in host.window.posts], ["connect"])
        self.assertFalse(host.window.is_open())
        self.assertIsNone(exchange._pending_token)
        self.assertIsNone(exchange._print_token)

    def test_redirected_page_cannot_forge_a_native_receipt(self):
        for token in (None, "wrong-session", 42):
            with self.subTest(token=token):
                host = FakeHost(lambda window, _: window.send("submitted", access_token=token))
                with self.assertRaisesRegex(ProtocolError, "unverified print receipt"):
                    make_exchange(host, "https://grove.example", "print", {}).run()
                self.assertNotIn("browser-token", repr(host.window.posts))

    def test_login_failure_or_cancellation_can_finish_before_authentication(self):
        for kind in ("cancelled", "error"):
            with self.subTest(kind=kind):
                host = FakeHost(authenticate=False)
                original_post = FakeWindow.post

                def fail(window, data):
                    original_post(window, data)
                    window.send(kind)

                from unittest.mock import patch

                with patch.object(FakeWindow, "post", fail):
                    self.assertEqual(
                        make_exchange(host, "https://grove.example", "print", {}).run()["type"], kind
                    )

    def test_print_reload_reauthenticates_without_uploading_or_automatically_submitting(self):
        initialized = []

        def respond(window, data):
            initialized.append(data)
            if len(initialized) == 1:
                window.send("ready", mode="connect")
            else:
                window.send("submitted", queue_item_ids=[1])

        host = FakeHost(respond)
        result = make_exchange(host, "https://grove.example", "print", {"library_file_id": 42}).run()
        self.assertEqual(result["type"], "submitted")
        self.assertEqual(len(initialized), 2)
        self.assertEqual(initialized[0], initialized[1])

    def test_print_never_sends_a_session_token_to_the_page(self):
        host = FakeHost(lambda window, _: window.send("submitted", queue_item_ids=[1]))
        exchange = make_exchange(host, "https://grove.example", "print", {"access_token": "private-token"})
        self.assertEqual(exchange.run()["queue_item_ids"], [1])
        self.assertNotIn("private-token", host.url)
        self.assertNotIn("private-token", host.created[0]["html"])
        self.assertEqual(parse_qs(urlsplit(host.url).query)["transport"], ["native"])
        self.assertNotIn("iframe", host.created[0]["html"])
        self.assertNotIn("private-token", repr(host.window.posts))
        self.assertNotIn("browser-token", repr(host.window.posts))
        self.assertIsNone(exchange._print_token)
        self.assertEqual(exchange._initialization, {})
        self.assertFalse(host.window.is_open())
        self.assertEqual(host.created[0]["style"], host.WINDOW_MODAL)

    def test_modal_print_window_keeps_send_pending_until_grove_receipt(self):
        initialized = threading.Event()
        host = FakeHost(lambda *_: initialized.set())
        exchange = make_exchange(host, "https://grove.example", "print", {})
        outcomes, failures = [], []

        def run():
            try:
                outcomes.append(exchange.run())
            except Exception as exc:
                failures.append(exc)

        worker = threading.Thread(target=run)
        worker.start()
        try:
            self.assertTrue(initialized.wait(2))
            self.assertEqual(host.created[0]["style"], host.WINDOW_MODAL)
            self.assertTrue(host.window.is_open())
            self.assertTrue(worker.is_alive())
            self.assertEqual(outcomes, [])
            host.window.send("submitted", queue_item_ids=[1])
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(outcomes[0]["type"], "submitted")
            self.assertFalse(host.window.is_open())
        finally:
            exchange.interrupt()
            worker.join(2)

    def test_cancel_request_can_finish_a_modal_print_exchange(self):
        cancelled = threading.Event()

        def respond(window, data):
            if data["type"] == "initialize":
                cancelled.set()
            elif data["type"] == "cancel_requested":
                window.send("cancelled")

        host = FakeHost(respond)
        result = make_exchange(host, "https://grove.example", "print", {}, cancelled=cancelled.is_set).run()
        self.assertEqual(host.created[0]["style"], host.WINDOW_MODAL)
        self.assertEqual(result["type"], "cancelled")
        self.assertEqual(
            [data["type"] for data in host.window.posts], ["initialize", "initialize", "cancel_requested"]
        )
        self.assertFalse(host.window.is_open())

    def test_stale_operation_cannot_complete_current_operation(self):
        def complete(window, _):
            window.send("authenticated", request_id="another-operation", access_token="wrong-token")
            window.send("authenticated", access_token="right-token")

        host = FakeHost(complete)
        result = make_exchange(host, "https://grove.example", "connect", {}).run()
        self.assertEqual(result["access_token"], "right-token")
        self.assertEqual(host.created[0]["style"], host.WINDOW_MODELESS)

    def test_protocol_version_mismatch_fails(self):
        host = FakeHost(lambda window, _: window.send("authenticated", protocol_version=2))
        with self.assertRaises(ProtocolError):
            make_exchange(host, "https://grove.example", "connect", {}).run()

    def test_authentication_event_cannot_finish_print_flow(self):
        host = FakeHost(lambda window, _: window.send("authenticated", access_token="wrong-flow"))
        with self.assertRaises(ProtocolError):
            make_exchange(host, "https://grove.example", "print", {}).run()

    def test_missing_grove_route_times_out_with_actionable_error(self):
        for mode in ("connect", "print"):
            with self.subTest(mode=mode):
                host = FakeHost(ready=False)
                with self.assertRaisesRegex(ProtocolError, "Grove-side integration"):
                    make_exchange(host, "https://grove.example", mode, {}, handshake_timeout=0).run()
                self.assertFalse(host.window.is_open())

    def test_window_close_and_explicit_cancel_are_distinct(self):
        host = FakeHost(lambda window, _: window.close())
        self.assertEqual(make_exchange(host, "https://grove.example", "print", {}).run()["type"], "closed")
        host = FakeHost(lambda window, _: window.send("cancelled"))
        result = make_exchange(host, "https://grove.example", "print", {}).run()
        self.assertEqual(result["type"], "cancelled")

    def test_host_can_reenter_from_main_thread_before_post_returns(self):
        def complete(window, _):
            thread = threading.Thread(target=lambda: window.send("authenticated", access_token="token"))
            thread.start()
            thread.join(1)
            self.assertFalse(thread.is_alive(), "A worker must not hold a lock while calling host UI")

        host = FakeHost(complete)
        result = make_exchange(host, "https://grove.example", "connect", {}).run()
        self.assertEqual(result["type"], "authenticated")

    def test_cancel_before_open_creates_no_window(self):
        host = FakeHost()
        result = make_exchange(host, "https://grove.example", "print", {}, cancelled=lambda: True).run()
        self.assertEqual(result["type"], "interrupted")
        self.assertEqual(host.created, [])

    def test_unload_interrupts_a_waiting_window(self):
        host = FakeHost()
        exchange = make_exchange(host, "https://grove.example", "connect", {})
        timer = threading.Timer(0.05, exchange.interrupt)
        timer.start()
        self.assertEqual(exchange.run()["type"], "interrupted")
        timer.join()
        self.assertFalse(host.window.is_open())

    def test_navigation_script_cannot_be_terminated_by_url(self):
        html = navigation_html("https://example.com/</script><script>bad()</script>")
        self.assertEqual(html.count("</script>"), 1)
