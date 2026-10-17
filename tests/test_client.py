import json
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from grove_control_orca_plugin.client import GroveClient
from grove_control_orca_plugin.models import (
    AuthenticationRequired,
    Cancelled,
    GroveError,
    HostPermissionRequired,
    HttpError,
    ProtocolError,
    SlicedJob,
    normalize_url,
)


class Handler(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *_):
        pass

    def respond(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def do_GET(self):
        self.requests.append((self.path, dict(self.headers), b""))
        token = self.headers.get("Authorization", "")
        if token == "Bearer expired":
            self.respond(401, {"detail": "expired"})
        elif token == "Bearer redirect-secret":
            self.send_response(302)
            self.send_header("Location", "/steal-token")
            self.end_headers()
        elif token == "Bearer failed-secret":
            self.respond(503, {"detail": "failed-secret"})
        elif token == "Bearer bad-json":
            self.respond(200, [])
        elif self.path.endswith("/auth/status"):
            self.respond(200, {"auth_enabled": True})
        else:
            self.respond(200, {"id": 7, "username": "alice", "is_active": True})

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.requests.append((self.path, dict(self.headers), body))
        self.respond(200, {"id": 42})


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/grove"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        Handler.requests = []
        self.client = GroveClient(self.url)

    def test_user_session_and_reverse_proxy_base_path(self):
        self.client.require_user_auth()
        self.assertEqual(self.client.account("session-token").username, "alice")
        path, headers, _ = Handler.requests[-1]
        self.assertEqual(path, "/grove/api/v1/auth/me")
        self.assertEqual(headers["Authorization"], "Bearer session-token")
        self.assertNotIn("session-token", path)

    def test_binary_upload_is_complete_and_reports_file_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pièce.gcode.3mf"
            payload = bytes(range(256)) * 3000
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
                archive.writestr("3D/mesh.bin", payload)
                archive.writestr("Metadata/plate_1.gcode", b"G28\n")
                archive.writestr("Metadata/plate_1.gcode.md5", b"hash")
                archive.writestr("Metadata/plate_1.json", b"{}")
            content = path.read_bytes()
            progress = []
            self.assertEqual(self.client.upload(SlicedJob(path), "session-token", progress.append), 42)
        path, headers, body = Handler.requests[-1]
        self.assertEqual(path, "/grove/api/v1/library/files")
        self.assertEqual(headers["Authorization"], "Bearer session-token")
        self.assertEqual(int(headers["Content-Length"]), len(body))
        self.assertIn('filename="pièce.gcode.3mf"'.encode(), body)
        self.assertIn(b"\r\n\r\n" + content + b"\r\n--", body)
        self.assertEqual(progress[0].completed_bytes, 0)
        self.assertEqual(progress[-1].completed_bytes, len(content))
        self.assertGreater(len(progress), 3)

    def test_cancel_before_upload_sends_no_request(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "part.gcode.3mf"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("Metadata/plate_1.gcode", b"G28\n")
                archive.writestr("Metadata/plate_1.gcode.md5", b"hash")
                archive.writestr("Metadata/plate_1.json", b"{}")
            with self.assertRaises(Cancelled):
                self.client.upload(SlicedJob(path), "token", cancelled=lambda: True)
        self.assertEqual(Handler.requests, [])

    def test_native_3mf_upload_uses_project_name_and_unchanged_archive_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".52871.0.3mf"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("Metadata/plate_3.gcode", b"G28\n")
                archive.writestr("Metadata/plate_3.gcode.md5", b"hash")
                archive.writestr("Metadata/plate_3.json", b"{}")
            original = path.read_bytes()
            job = SlicedJob.from_native(path, "3DBenchy", plate_id=3)
            self.assertEqual(self.client.upload(job, "session-token"), 42)
            self.assertEqual(path.read_bytes(), original)
        self.assertEqual(len(Handler.requests), 1)
        body = Handler.requests[0][2]
        self.assertIn(b'filename="3DBenchy.gcode.3mf"', body)
        self.assertNotIn(b".52871.0.3mf", body)
        self.assertIn(b"\r\n\r\n" + original + b"\r\n--", body)

    def test_unauthenticated_response_requires_new_login(self):
        with self.assertRaises(AuthenticationRequired):
            self.client.account("expired")

    def test_orca_network_permission_denial_is_distinct_and_does_not_leak_details(self):
        with patch.object(self.client, "_connection", side_effect=PermissionError("sensitive-details")):
            with self.assertRaises(HostPermissionRequired) as raised:
                self.client.account("user-session-secret")
        self.assertIn("Orca blocked permission", str(raised.exception))
        self.assertNotIn("sensitive-details", str(raised.exception))
        self.assertNotIn("user-session-secret", str(raised.exception))
        self.assertEqual(Handler.requests, [])

    def test_orca_denial_during_upload_closes_connection_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "part.gcode.3mf"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("Metadata/plate_1.gcode", b"G28\n")
                archive.writestr("Metadata/plate_1.gcode.md5", b"hash")
                archive.writestr("Metadata/plate_1.json", b"{}")
            with patch.object(self.client, "_connection") as connect:
                connection = connect.return_value
                connection.endheaders.side_effect = PermissionError("sensitive-network-details")
                with self.assertRaises(HostPermissionRequired) as raised:
                    self.client.upload(SlicedJob(path), "user-session-secret")
                connect.assert_called_once()
                connection.close.assert_called_once()
        self.assertNotIn("sensitive-network-details", str(raised.exception))
        self.assertNotIn("user-session-secret", str(raised.exception))
        self.assertEqual(Handler.requests, [])

    def test_redirect_is_not_followed_with_user_token(self):
        with self.assertRaises(HttpError) as raised:
            self.client.account("redirect-secret")
        self.assertEqual(raised.exception.status, 302)
        self.assertEqual(len(Handler.requests), 1)

    def test_error_does_not_echo_secrets_or_retry(self):
        with self.assertRaises(HttpError) as raised:
            self.client.account("failed-secret")
        self.assertNotIn("failed-secret", str(raised.exception))
        self.assertEqual(len(Handler.requests), 1)

    def test_api_keys_are_rejected_before_network_access(self):
        with self.assertRaises(AuthenticationRequired):
            self.client.account("bb_not-a-user-session")
        self.assertEqual(Handler.requests, [])

    def test_unexpected_response_is_rejected(self):
        with self.assertRaises(ProtocolError):
            self.client.account("bad-json")

    def test_only_sliced_filenames_and_positive_plate_ids_are_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "project.3mf"
            path.write_text("project")
            with self.assertRaises(GroveError):
                SlicedJob(path).validate()
            raw = Path(directory) / "part.gcode"
            raw.write_text("G28\n")
            with self.assertRaisesRegex(GroveError, "sliced .gcode.3mf"):
                SlicedJob(raw).validate()
            with self.assertRaises(GroveError):
                SlicedJob(path, "part.gcode.3mf", plate_id=0).validate()
            with self.assertRaises(GroveError):
                SlicedJob(path, "../part.gcode").validate()

    def test_server_url_has_no_credential_or_query_channels(self):
        self.assertEqual(normalize_url(" HTTPS://Grove.Example/grove/ "), "https://grove.example/grove")
        for value in (
            "grove.local",
            "http://user:secret@grove.local",
            "http://grove.local/?token=secret",
            "http://grove.local/#secret",
            "http://grove.local:bad",
            "http://[broken-ipv6",
            "http://grove example",
            "http://grove.local/\n",
        ):
            with self.subTest(value=value), self.assertRaises(GroveError):
                normalize_url(value)
