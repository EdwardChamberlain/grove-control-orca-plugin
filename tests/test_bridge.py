import json
import re
import shutil
import subprocess
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

from grove_control_orca_plugin.bridge import RemoteExchange, navigation_html, print_frame_html
from grove_control_orca_plugin.models import ProtocolError


class FakeWindow:
    def __init__(self, host, callbacks):
        self.host = host
        self.callbacks = callbacks
        self.open = True
        self.posts = []

    def post(self, data):
        self.posts.append(data)
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
                **data,
            }
        )


class FakeHost:
    WINDOW_MODELESS = 0
    WINDOW_MODAL = 1

    def __init__(self, on_post=lambda *_: None, ready=True):
        self.on_post = on_post
        self.ready = ready
        self.created = []

    def create_window(self, **callbacks):
        self.created.append(callbacks)
        url_match = re.search(r"replace\((.*)\);", callbacks["html"]) or re.search(
            r"new URL\((.*?)\);", callbacks["html"]
        )
        self.url = json.loads(url_match.group(1))
        self.request_id = parse_qs(urlsplit(self.url).query)["request_id"][0]
        self.window = FakeWindow(self, callbacks)
        if self.ready:
            # Exercise the race where ready arrives before create_window returns a handle.
            self.window.send("ready")
        return self.window


class BridgeTests(unittest.TestCase):
    def test_token_travels_only_in_initialization_after_ready(self):
        host = FakeHost(lambda window, _: window.send("submitted", queue_item_ids=[1]))
        exchange = RemoteExchange(host, "https://grove.example", "print", {"access_token": "private-token"})
        self.assertEqual(exchange.run()["queue_item_ids"], [1])
        self.assertNotIn("private-token", host.url)
        self.assertNotIn("private-token", host.created[0]["html"])
        self.assertEqual(host.window.posts[0]["access_token"], "private-token")
        self.assertEqual(exchange._initialization, {})
        self.assertFalse(host.window.is_open())
        self.assertEqual(host.created[0]["style"], host.WINDOW_MODAL)

    def test_modal_print_window_keeps_send_pending_until_grove_receipt(self):
        initialized = threading.Event()
        host = FakeHost(lambda *_: initialized.set())
        exchange = RemoteExchange(host, "https://grove.example", "print", {})
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
        result = RemoteExchange(host, "https://grove.example", "print", {}, cancelled=cancelled.is_set).run()
        self.assertEqual(host.created[0]["style"], host.WINDOW_MODAL)
        self.assertEqual(result["type"], "cancelled")
        self.assertEqual([data["type"] for data in host.window.posts], ["initialize", "cancel_requested"])
        self.assertFalse(host.window.is_open())

    def test_stale_operation_cannot_complete_current_operation(self):
        def complete(window, _):
            window.send("authenticated", request_id="another-operation", access_token="wrong-token")
            window.send("authenticated", access_token="right-token")

        host = FakeHost(complete)
        result = RemoteExchange(host, "https://grove.example", "connect", {}).run()
        self.assertEqual(result["access_token"], "right-token")
        self.assertEqual(host.created[0]["style"], host.WINDOW_MODELESS)

    def test_protocol_version_mismatch_fails(self):
        host = FakeHost(lambda window, _: window.send("authenticated", protocol_version=2))
        with self.assertRaises(ProtocolError):
            RemoteExchange(host, "https://grove.example", "connect", {}).run()

    def test_authentication_event_cannot_finish_print_flow(self):
        host = FakeHost(lambda window, _: window.send("authenticated", access_token="wrong-flow"))
        with self.assertRaises(ProtocolError):
            RemoteExchange(host, "https://grove.example", "print", {}).run()

    def test_missing_grove_route_times_out_with_actionable_error(self):
        for mode in ("connect", "print"):
            with self.subTest(mode=mode):
                host = FakeHost(ready=False)
                with self.assertRaisesRegex(ProtocolError, "Grove-side integration"):
                    RemoteExchange(host, "https://grove.example", mode, {}, handshake_timeout=0).run()
                self.assertFalse(host.window.is_open())

    def test_window_close_and_explicit_cancel_are_distinct(self):
        host = FakeHost(lambda window, _: window.close())
        self.assertEqual(RemoteExchange(host, "https://grove.example", "print", {}).run()["type"], "closed")
        host = FakeHost(lambda window, _: window.send("cancelled"))
        result = RemoteExchange(host, "https://grove.example", "print", {}).run()
        self.assertEqual(result["type"], "cancelled")

    def test_host_can_reenter_from_main_thread_before_post_returns(self):
        def complete(window, _):
            thread = threading.Thread(target=lambda: window.send("authenticated", access_token="token"))
            thread.start()
            thread.join(1)
            self.assertFalse(thread.is_alive(), "A worker must not hold a lock while calling host UI")

        host = FakeHost(complete)
        result = RemoteExchange(host, "https://grove.example", "connect", {}).run()
        self.assertEqual(result["type"], "authenticated")

    def test_cancel_before_open_creates_no_window(self):
        host = FakeHost()
        result = RemoteExchange(host, "https://grove.example", "print", {}, cancelled=lambda: True).run()
        self.assertEqual(result["type"], "interrupted")
        self.assertEqual(host.created, [])

    def test_unload_interrupts_a_waiting_window(self):
        host = FakeHost()
        exchange = RemoteExchange(host, "https://grove.example", "connect", {})
        timer = threading.Timer(0.05, exchange.interrupt)
        timer.start()
        self.assertEqual(exchange.run()["type"], "interrupted")
        timer.join()
        self.assertFalse(host.window.is_open())

    def test_navigation_script_cannot_be_terminated_by_url(self):
        html = navigation_html("https://example.com/</script><script>bad()</script>")
        self.assertEqual(html.count("</script>"), 1)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise the browser bridge policy")
    def test_print_frame_enforces_origin_source_and_target_origin(self):
        html = print_frame_html("https://grove.example:443/integrations/orca?request_id=example")
        script = re.search(r"<script>(.*?)</script>", html).group(1)
        check = """
const vm=require('node:vm');
let incoming, outgoing;
const sent=[], forwarded=[];
const frame={contentWindow:{postMessage:(data,origin)=>sent.push({data,origin})}};
vm.runInNewContext(JSON.parse(require('node:fs').readFileSync(0,'utf8')), {
  URL, document:{getElementById:()=>frame}, window:{
    addEventListener:(_,cb)=>incoming=cb,
    orca:{postMessage:data=>forwarded.push(data),onMessage:cb=>outgoing=cb}
  }
});
incoming({origin:'https://attacker.example',source:frame.contentWindow,data:{type:'ready'}});
incoming({origin:'https://grove.example',source:{},data:{type:'ready'}});
if(forwarded.length) throw new Error('Untrusted frame reached native bridge');
incoming({origin:'https://grove.example',source:frame.contentWindow,data:{type:'ready'}});
outgoing({access_token:'private-token'});
if(forwarded.length!==1 || sent[0].origin!=='https://grove.example') throw new Error('Origin policy failed');
"""
        subprocess.run(
            ["node", "-e", check], input=json.dumps(script), text=True, check=True, capture_output=True
        )
