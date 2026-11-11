import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

from grove_control_orca_plugin.models import (
    Account,
    AuthenticationRequired,
    Cancelled,
    GroveError,
    HostPermissionRequired,
    Progress,
    SlicedJob,
    SubmissionStatus,
)
from grove_control_orca_plugin.runtime import Runtime


class FakeClient:
    def __init__(self):
        self.user = Account(7, "alice")
        self.uploads = []
        self.revoked = []
        self.auth_error = None
        self.upload_error = None
        self.on_upload = lambda: None

    def require_user_auth(self, *, timeout=None):
        self.last_auth_timeout = timeout
        if self.auth_error:
            raise self.auth_error

    def account(self, token):
        return self.user

    def upload(self, job, token, progress, cancelled):
        job = job.validate()
        self.uploads.append((job, token))
        total = job.path.stat().st_size
        progress(Progress("uploading", 0, total))
        self.on_upload()
        if cancelled():
            raise Cancelled("Upload cancelled.")
        if self.upload_error:
            raise self.upload_error
        progress(Progress("uploading", total, total))
        return 42

    def logout(self, token):
        self.revoked.append(token)


class FakeExchange:
    def __init__(self, event, initialization):
        self.event = event
        self.initialization = initialization
        self.opened = False

    def run(self):
        self.opened = True
        return self.event

    def interrupt(self):
        pass


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "part.gcode.3mf"
        with zipfile.ZipFile(self.path, "w") as archive:
            for plate_id in (1, 2):
                archive.writestr(f"Metadata/plate_{plate_id}.gcode", b"G28\n")
                archive.writestr(f"Metadata/plate_{plate_id}.gcode.md5", b"hash")
                archive.writestr(f"Metadata/plate_{plate_id}.json", b"{}")
        self.job = SlicedJob(self.path, plate_id=2)
        self.client = FakeClient()
        self.events = [{"type": "authenticated", "access_token": "user-session-secret"}]
        self.exchanges = []

        def exchange_factory(host, url, mode, initialization, cancelled, **kwargs):
            exchange = FakeExchange(self.events.pop(0), initialization)
            exchange.verify_print_session = kwargs.get("verify_print_session")
            self.exchanges.append(exchange)
            return exchange

        self.runtime = Runtime(
            object(),
            "https://grove.example",
            client_factory=lambda _: self.client,
            exchange_factory=exchange_factory,
        )
        self.runtime.sign_in()

    def receipt(self, kind="submitted", **changes):
        self.events.append(
            {
                "type": kind,
                "library_file_id": 42,
                "user_id": 7,
                "queue_item_ids": [101, 102],
                **changes,
            }
        )

    def test_login_is_separate_and_keeps_tokens_out_of_public_state(self):
        state = self.runtime.snapshot()
        self.assertEqual(state["account"], {"id": 7, "username": "alice"})
        self.assertNotIn("user-session-secret", repr(state))
        self.assertNotIn("user-session-secret", repr(self.runtime._session))
        self.assertEqual(self.client.uploads, [])
        self.assertTrue(state["ready_for_print"])
        self.assertTrue(self.runtime.ready_for_print())

    def test_send_reuses_account_and_delegates_options_and_queue_writes_to_grove(self):
        self.receipt()
        phases = []
        result = self.runtime.submit_job(self.job, lambda value: phases.append(value.phase))
        self.assertEqual(result.status, SubmissionStatus.SUBMITTED)
        self.assertEqual(result.queue_item_ids, (101, 102))
        initialization = self.exchanges[-1].initialization
        self.assertEqual(initialization["expected_user_id"], 7)
        self.assertEqual(initialization["plate_id"], 2)
        self.assertEqual(initialization["library_file_id"], 42)
        self.assertNotIn("printer_id", initialization)
        self.assertNotIn("access_token", initialization)
        self.exchanges[-1].verify_print_session("browser-session-token")
        self.assertEqual(len(self.client.uploads), 1)
        self.assertEqual(phases, ["validating", "uploading", "uploading", "configuring"])

    def test_print_browser_session_must_match_the_upload_account(self):
        self.receipt()
        self.runtime.submit_job(self.job)
        self.client.user = Account(8, "bob")
        with self.assertRaisesRegex(AuthenticationRequired, "same account"):
            self.exchanges[-1].verify_print_session("other-user-token")

    def test_native_upload_name_and_selected_plate_reach_the_grove_form(self):
        path = self.path.with_name(".52871.0.3mf")
        path.write_bytes(self.path.read_bytes())
        job = SlicedJob.from_native(path, "3DBenchy", plate_id=2)
        self.receipt()
        result = self.runtime.submit_job(job)
        self.assertEqual(result.status, SubmissionStatus.SUBMITTED)
        self.assertEqual(self.client.uploads[0][0].path, path)
        self.assertEqual(self.client.uploads[0][0].filename, "3DBenchy.gcode.3mf")
        self.assertEqual(self.exchanges[-1].initialization["filename"], "3DBenchy.gcode.3mf")
        self.assertEqual(self.exchanges[-1].initialization["plate_id"], 2)

    def test_upload_permission_error_fails_without_opening_print_form(self):
        self.client.upload_error = GroveError("Grove returned HTTP 403.")
        result = self.runtime.submit_job(self.job)
        self.assertEqual(result.status, SubmissionStatus.FAILED)
        self.assertIn("403", result.message)
        self.assertEqual(len(self.client.uploads), 1)
        self.assertEqual(len(self.exchanges), 1)  # Only the separate sign-in form.
        self.assertFalse(self.runtime.ready_for_print())

    def test_access_preparation_only_reads_status_and_archive(self):
        self.runtime.prepare_access(self.job)
        self.assertEqual(self.client.last_auth_timeout, 3)
        self.assertEqual(self.client.uploads, [])
        self.assertEqual(len(self.exchanges), 1)

    def test_orca_permission_denial_stops_submission_before_upload(self):
        def deny(job, cancelled):
            raise HostPermissionRequired()

        self.runtime._prepare_job = deny
        result = self.runtime.submit_job(self.job)
        self.assertEqual(result.status, SubmissionStatus.FAILED)
        self.assertIn("Orca blocked permission", result.message)
        self.assertEqual(self.client.uploads, [])
        self.assertEqual(len(self.exchanges), 1)
        self.assertFalse(self.runtime.snapshot()["busy"])

    def test_missing_plate_is_rejected_before_upload(self):
        result = self.runtime.submit_job(SlicedJob(self.path, plate_id=9))
        self.assertEqual(result.status, SubmissionStatus.FAILED)
        self.assertEqual(self.client.uploads, [])

    def test_account_change_is_detected_before_upload(self):
        self.client.user = Account(8, "bob")
        result = self.runtime.submit_job(self.job)
        self.assertEqual(result.status, SubmissionStatus.FAILED)
        self.assertEqual(self.client.uploads, [])
        self.assertIsNone(self.runtime.snapshot()["account"])
        self.assertFalse(self.runtime.ready_for_print())

    def test_disabled_authentication_never_uploads_an_anonymous_job(self):
        self.client.auth_error = AuthenticationRequired("Enable user authentication")
        result = self.runtime.submit_job(self.job)
        self.assertEqual(result.status, SubmissionStatus.FAILED)
        self.assertEqual(self.client.uploads, [])
        self.assertFalse(self.runtime.ready_for_print())

    def test_wrong_receipt_account_file_or_ids_are_unknown_not_success(self):
        for changes in (
            {"user_id": 8},
            {"library_file_id": 43},
            {"queue_item_ids": []},
            {"queue_item_ids": [True]},
            {"queue_item_ids": [1, 1]},
        ):
            with self.subTest(changes=changes):
                self.receipt(**changes)
                result = self.runtime.submit_job(self.job)
                self.assertEqual(result.status, SubmissionStatus.UNKNOWN)
                self.assertEqual(result.library_file_id, 42)

    def test_partial_success_preserves_ids_and_does_not_resubmit(self):
        self.receipt("partial", queue_item_ids=[101])
        result = self.runtime.submit_job(self.job)
        self.assertEqual(result.status, SubmissionStatus.PARTIAL)
        self.assertEqual(result.queue_item_ids, (101,))
        self.assertEqual(len(self.client.uploads), 1)

    def test_closed_window_is_unknown_and_does_not_resubmit(self):
        self.events.append({"type": "closed"})
        result = self.runtime.submit_job(self.job)
        self.assertEqual(result.status, SubmissionStatus.UNKNOWN)
        self.assertIn("may already be queued", result.message)
        self.assertEqual(len(self.client.uploads), 1)

    def test_explicit_cancel_leaves_uploaded_source_without_queue_receipt(self):
        self.events.append({"type": "cancelled"})
        result = self.runtime.submit_job(self.job)
        self.assertEqual(result.status, SubmissionStatus.CANCELLED)
        self.assertEqual(result.library_file_id, 42)
        self.assertEqual(result.queue_item_ids, ())

    def test_cancel_before_upload_does_not_upload_or_open_print_window(self):
        result = self.runtime.submit_job(self.job, cancelled=lambda: True)
        self.assertEqual(result.status, SubmissionStatus.CANCELLED)
        self.assertEqual(self.client.uploads, [])
        self.assertEqual(len(self.exchanges), 1)  # Only the prior login.

    def test_expired_session_in_form_is_unknown_and_clears_login(self):
        self.events.append({"type": "session_expired"})
        result = self.runtime.submit_job(self.job)
        self.assertEqual(result.status, SubmissionStatus.UNKNOWN)
        self.assertIsNone(self.runtime.snapshot()["account"])

    def test_signout_revokes_only_the_session_and_requires_login_before_sending(self):
        self.runtime.sign_out()
        self.assertEqual(self.client.revoked, ["user-session-secret"])
        result = self.runtime.submit_job(self.job)
        self.assertEqual(result.status, SubmissionStatus.FAILED)
        self.assertEqual(self.client.uploads, [])

    def test_failed_config_save_preserves_current_connection(self):
        with self.assertRaisesRegex(GroveError, "could not save"):
            self.runtime.configure("https://other.example", persist=lambda _: False)
        self.assertEqual(self.runtime.snapshot()["grove_url"], "https://grove.example")
        self.assertIsNotNone(self.runtime.snapshot()["account"])

    def test_changing_server_clears_authentication(self):
        saved = []
        self.runtime.configure("https://other.example", persist=lambda url: saved.append(url) or True)
        self.assertEqual(saved, ["https://other.example"])
        self.assertIsNone(self.runtime.snapshot()["account"])

    def test_server_and_account_cannot_change_mid_submission(self):
        entered, release = threading.Event(), threading.Event()
        self.client.on_upload = lambda: (entered.set(), release.wait(3))
        self.receipt()
        worker = threading.Thread(target=self.runtime.submit_job, args=(self.job,))
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            with self.assertRaises(GroveError):
                self.runtime.configure("https://other.example")
            with self.assertRaises(GroveError):
                self.runtime.sign_out()
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.runtime.snapshot()["last_submission"]["status"], "submitted")

    def test_unload_clears_session_and_blocks_new_operations(self):
        self.runtime.close()
        self.assertIsNone(self.runtime.snapshot()["account"])
        with self.assertRaises(GroveError):
            self.runtime.submit_job(self.job)
