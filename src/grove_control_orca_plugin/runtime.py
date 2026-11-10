"""Connection/session lifecycle and native PrinterAgent job submission."""

from __future__ import annotations

import threading
import time
from dataclasses import asdict
from typing import Callable

from .bridge import RemoteExchange, WindowHost
from .client import GroveClient
from .models import (
    AuthenticationRequired,
    CancelCallback,
    Cancelled,
    GroveError,
    HostPermissionRequired,
    Progress,
    ProgressCallback,
    ProtocolError,
    Session,
    SlicedJob,
    SubmissionResult,
    SubmissionStatus,
    normalize_url,
    positive_id,
)


class Runtime:
    def __init__(
        self,
        host: WindowHost,
        grove_url: str = "",
        client_factory: Callable[[str], GroveClient] = GroveClient,
        exchange_factory=RemoteExchange,
        changed: Callable[[dict], None] = lambda _: None,
        prepare_job: Callable[[SlicedJob, CancelCallback], None] = lambda _job, _cancelled: None,
    ):
        self._host = host
        self._url = normalize_url(grove_url) if grove_url else ""
        self._client_factory = client_factory
        self._exchange_factory = exchange_factory
        self._changed = changed
        self._prepare_job = prepare_job
        self._session: Session | None = None
        self._guard = threading.RLock()
        self._busy = False
        self._stopped = threading.Event()
        self._exchange: RemoteExchange | None = None
        self._message = "Sign in to Grove to attribute uploads and jobs to your account."
        self._last_result: SubmissionResult | None = None
        self._progress: Progress | None = None
        self._connection_verified = False
        self._last_connection_check = 0.0

    def snapshot(self) -> dict:
        with self._guard:
            return {
                "grove_url": self._url,
                "account": asdict(self._session.account) if self._session else None,
                "busy": self._busy,
                "message": self._message,
                "last_submission": asdict(self._last_result) if self._last_result else None,
                "connection_verified": self._connection_verified,
                "progress": asdict(self._progress) if self._progress else None,
                "ready_for_print": self._ready_for_print_locked(),
            }

    def _ready_for_print_locked(self) -> bool:
        return bool(
            not self._stopped.is_set()
            and not self._busy
            and self._url
            and self._session
            and self._connection_verified
        )

    def ready_for_print(self) -> bool:
        """Whether a native print callback can begin an authenticated Grove upload.

        Grove does not expose upload-permission preflight, so only the upload
        response can establish that the account is permitted to submit files.
        """
        with self._guard:
            return self._ready_for_print_locked()

    def prepare_access(self, job: SlicedJob | None = None) -> None:
        """Read-only permission preparation, called through Orca's audited UI entry.

        Orca denies a first permission request on a worker while scheduling its
        prompt asynchronously. Perform a bounded real API read on the UI thread
        so its normal permission prompt can finish before any upload begins.
        The job probe requests read access to only the host-supplied archive.
        """
        with self._guard:
            url = self._url
        if not url:
            raise GroveError("Configure your Grove URL first.")
        self._client_factory(url).require_user_auth(timeout=3)
        if job is not None:
            try:
                with job.path.open("rb") as source:
                    source.read(1)
            except PermissionError as exc:
                raise HostPermissionRequired("read the sliced archive") from exc
            except OSError as exc:
                raise GroveError("The sliced archive supplied by Orca is no longer readable.") from exc

    def connection_check_due(self, max_age_seconds: float = 60) -> bool:
        with self._guard:
            return (
                self._session is not None
                and not self._busy
                and time.monotonic() - self._last_connection_check >= max_age_seconds
            )

    def _publish(self, message: str | None = None) -> None:
        with self._guard:
            if message is not None:
                self._message = message
        if not self._stopped.is_set():
            self._changed(self.snapshot())

    def configure(self, grove_url: str, persist: Callable[[str], bool] | None = None) -> str:
        url = normalize_url(grove_url)
        with self._guard:
            if self._busy:
                raise GroveError("Finish the current Grove action before changing the server.")
            if persist is not None and not persist(url):
                raise GroveError("Orca could not save the Grove URL.")
            if url != self._url:
                self._session = None
                self._last_result = None
                self._connection_verified = False
                self._last_connection_check = 0.0
            self._url = url
            message = (
                f"Signed in as {self._session.account.username}."
                if self._session
                else "Grove URL saved. Sign in to connect."
            )
        self._publish(message)
        return url

    def _begin(self) -> str:
        with self._guard:
            if self._stopped.is_set():
                raise GroveError("The Grove plugin has been unloaded.")
            if self._busy:
                raise GroveError("A Grove action is already in progress.")
            if not self._url:
                raise GroveError("Configure your Grove URL first.")
            self._busy = True
            return self._url

    def _end(self) -> None:
        with self._guard:
            self._busy = False
            self._exchange = None
            self._progress = None
        self._publish()

    def sign_in(self) -> None:
        """Run on a worker thread; credentials are entered only on Grove's own page."""
        url = self._begin()
        try:
            self._publish("Opening Grove sign-in…")
            client = self._client_factory(url)
            client.require_user_auth()
            exchange = self._exchange_factory(self._host, url, "connect", {}, self._stopped.is_set)
            self._exchange = exchange
            event = exchange.run()
            if event.get("type") in ("cancelled", "closed", "interrupted"):
                self._publish("Sign-in cancelled.")
                return
            token = event.get("access_token")
            if event.get("type") != "authenticated" or not isinstance(token, str) or not token:
                raise AuthenticationRequired("Grove did not return a user login session.")
            account = client.account(token)
            if self._stopped.is_set():
                return
            with self._guard:
                self._session = Session(url, account, token)
                self._connection_verified = True
                self._last_connection_check = time.monotonic()
            self._publish(f"Signed in as {account.username}.")
        except GroveError as exc:
            self._publish(str(exc))
            raise
        finally:
            self._end()

    def sign_out(self) -> None:
        self._begin()
        try:
            with self._guard:
                session, self._session = self._session, None
                self._connection_verified = False
                self._last_connection_check = 0.0
            self._publish("Signed out.")
            if session:
                try:
                    self._client_factory(session.grove_url).logout(session.token)
                except GroveError:
                    self._publish("Signed out locally. Grove could not confirm session revocation.")
        finally:
            self._end()

    def check_connection(self) -> None:
        """Recheck the saved Grove session on a worker thread."""
        url = self._begin()
        try:
            with self._guard:
                session = self._session
                self._connection_verified = False
            self._publish()
            if session is None:
                raise AuthenticationRequired("Sign in to Grove before checking the connection.")
            client = self._client_factory(url)
            client.require_user_auth()
            if client.account(session.token) != session.account:
                raise AuthenticationRequired("The Grove account changed. Sign in again before sending.")
            with self._guard:
                self._connection_verified = True
                self._last_connection_check = time.monotonic()
            self._publish("Grove connection verified. Upload permission is checked when sending.")
        except AuthenticationRequired as exc:
            with self._guard:
                self._session = None
                self._connection_verified = False
                self._last_connection_check = 0.0
            self._publish(str(exc))
            raise
        except GroveError as exc:
            with self._guard:
                self._connection_verified = False
                self._last_connection_check = time.monotonic()
            self._publish("Could not verify Grove. Check the server connection before sending.")
            raise exc
        finally:
            self._end()

    def submit_job(
        self,
        job: SlicedJob,
        progress: ProgressCallback = lambda _: None,
        cancelled: CancelCallback = lambda: False,
    ) -> SubmissionResult:
        """Worker-thread entry point used by Orca's native PrinterAgent callback.

        Grove owns all destination selection and queue writes. This never queries
        printer readiness or makes a queue POST, and never retries a submission.
        """
        url = self._begin()
        file_id: int | None = None
        exchange: RemoteExchange | None = None
        result: SubmissionResult | None = None
        try:
            session = self._session
            if session is None:
                raise AuthenticationRequired("Open Grove in Orca and sign in before sending a job.")
            # GroveClient validates the native archive once, immediately before
            # constructing the upload. Normalize its display name here so the
            # subsequent Print-form handoff uses that same name.
            job = SlicedJob(job.path, job.filename or job.path.name, job.plate_id)

            def cancel():
                return self._stopped.is_set() or cancelled()

            if cancel():
                raise Cancelled("Submission cancelled.")
            self._publish("Checking Orca permissions for Grove and the sliced archive…")
            self._prepare_job(job, cancel)
            if cancel():
                raise Cancelled("Submission cancelled before upload.")
            self._publish("Checking Grove session…")
            self._report_progress(Progress("validating"), progress)
            client = self._client_factory(url)
            client.require_user_auth()
            if client.account(session.token) != session.account:
                raise AuthenticationRequired("The Grove account changed. Sign in again before sending.")
            with self._guard:
                self._connection_verified = True
                self._last_connection_check = time.monotonic()
            file_id = client.upload(
                job,
                session.token,
                lambda value: self._report_progress(value, progress),
                cancel,
            )
            if cancel():
                raise Cancelled("Submission cancelled. The uploaded file remains in Grove.")
            self._publish("Choose a destination and print options in Grove.")
            self._report_progress(Progress("configuring"), progress)
            exchange = self._exchange_factory(
                self._host,
                url,
                "print",
                {
                    "access_token": session.token,
                    "expected_user_id": session.account.id,
                    "library_file_id": file_id,
                    "filename": job.filename,
                    "plate_id": job.plate_id,
                },
                cancel,
            )
            self._exchange = exchange
            event = exchange.run()
            kind = event.get("type")
            if kind == "session_expired":
                raise AuthenticationRequired("Your Grove session expired. Sign in again before sending.")
            if kind in ("submitted", "partial"):
                receipt_file = positive_id(event.get("library_file_id"), "receipt file ID")
                receipt_user = positive_id(event.get("user_id"), "receipt user ID")
                if receipt_file != file_id or receipt_user != session.account.id:
                    raise ProtocolError("Grove returned a receipt for a different file or account.")
                ids = event.get("queue_item_ids")
                if not isinstance(ids, list) or not ids:
                    raise ProtocolError("Grove did not return the created queue item IDs.")
                queue_ids = tuple(positive_id(value, "queue item ID") for value in ids)
                if len(set(queue_ids)) != len(queue_ids):
                    raise ProtocolError("Grove returned duplicate queue item IDs.")
                status = SubmissionStatus.SUBMITTED if kind == "submitted" else SubmissionStatus.PARTIAL
                message = (
                    f"Added {len(queue_ids)} job(s) to Grove's queue."
                    if kind == "submitted"
                    else f"Grove queued {len(queue_ids)} job(s), but some requests failed. "
                    "Check Grove before retrying."
                )
                result = SubmissionResult(status, message, file_id, queue_ids)
            elif kind == "cancelled":
                result = SubmissionResult(
                    SubmissionStatus.CANCELLED, "Not queued. The uploaded file remains in Grove.", file_id
                )
            elif kind == "error":
                result = self._unknown(file_id, "Grove reported a submission error.")
            else:
                result = self._unknown(file_id)
        except AuthenticationRequired as exc:
            with self._guard:
                self._session = None
                self._connection_verified = False
                self._last_connection_check = 0.0
            result = (
                self._unknown(file_id, str(exc))
                if exchange is not None and exchange.opened
                else SubmissionResult(SubmissionStatus.FAILED, str(exc), file_id)
            )
        except Cancelled as exc:
            result = SubmissionResult(SubmissionStatus.CANCELLED, str(exc), file_id)
        except GroveError as exc:
            with self._guard:
                self._connection_verified = False
                self._last_connection_check = time.monotonic()
            result = (
                self._unknown(file_id, str(exc))
                if exchange is not None and exchange.opened
                else SubmissionResult(SubmissionStatus.FAILED, str(exc), file_id)
            )
        except Exception:
            # Keep the C++ PrinterAgent boundary safe and never expose local
            # paths, session data, or lower-level exception details in Orca UI.
            result = (
                self._unknown(file_id, "The Grove submission could not be confirmed.")
                if exchange is not None and exchange.opened
                else SubmissionResult(
                    SubmissionStatus.FAILED,
                    "The Grove submission could not be completed. Check the connection and try again.",
                    file_id,
                )
            )
        finally:
            if result is not None:
                with self._guard:
                    self._last_result = result
                    self._message = result.message
            self._end()
        assert result is not None
        return result

    def _report_progress(self, value: Progress, callback: ProgressCallback) -> None:
        with self._guard:
            previous = self._progress
            self._progress = value
            changed = previous is None or previous.phase != value.phase
            if value.phase == "uploading" and value.total_bytes > 0:
                old_percent = (
                    previous.completed_bytes * 100 // previous.total_bytes
                    if previous is not None and previous.phase == "uploading" and previous.total_bytes > 0
                    else -1
                )
                new_percent = value.completed_bytes * 100 // value.total_bytes
                changed = changed or new_percent != old_percent or value.completed_bytes == value.total_bytes
        if changed:
            self._publish()
        callback(value)

    @staticmethod
    def _unknown(
        file_id: int | None, reason: str = "The Grove window closed without a receipt."
    ) -> SubmissionResult:
        return SubmissionResult(
            SubmissionStatus.UNKNOWN,
            reason + " Check Grove's queue before sending again; the job may already be queued.",
            file_id,
        )

    def close(self) -> None:
        self._stopped.set()
        with self._guard:
            self._session = None
            self._progress = None
            self._connection_verified = False
            exchange = self._exchange
        if exchange:
            exchange.interrupt()
