"""Grove's existing HTTP API, with streaming uploads and no automatic retries."""

from __future__ import annotations

import http.client
import json
import os
import ssl
from urllib.parse import urlsplit
from uuid import uuid4

from .models import (
    Account,
    AuthenticationRequired,
    CancelCallback,
    Cancelled,
    GroveError,
    HostPermissionRequired,
    HttpError,
    Progress,
    ProgressCallback,
    ProtocolError,
    SlicedJob,
    normalize_url,
    positive_id,
)


class GroveClient:
    def __init__(self, grove_url: str, timeout: float = 30):
        self.grove_url = normalize_url(grove_url)
        self.timeout = timeout

    def _connection(self, timeout: float | None = None):
        url = urlsplit(self.grove_url)
        timeout = self.timeout if timeout is None else timeout
        if url.scheme == "https":
            return http.client.HTTPSConnection(
                url.hostname, url.port, timeout=timeout, context=ssl.create_default_context()
            )
        return http.client.HTTPConnection(url.hostname, url.port, timeout=timeout)

    def _path(self, endpoint: str) -> str:
        return urlsplit(self.grove_url).path + "/api/v1" + endpoint

    @staticmethod
    def _headers(token: str | None) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if token is not None:
            if not token or any(c in token for c in ("\r", "\n")) or token.startswith("bb_"):
                raise AuthenticationRequired("Sign in to Grove with a user account.")
            headers["Authorization"] = f"Bearer {token}"
        return headers

    @staticmethod
    def _response(connection) -> dict:
        response = connection.getresponse()
        if response.status == 401:
            raise AuthenticationRequired("Your Grove session expired. Sign in again.")
        if not 200 <= response.status < 300:
            # Do not follow redirects or echo response bodies: they may contain credentials.
            raise HttpError(response.status)
        payload = response.read(2 * 1024 * 1024 + 1)
        if len(payload) > 2 * 1024 * 1024:
            raise ProtocolError("Grove returned an unexpectedly large response.")
        try:
            data = json.loads(payload)
        except (ValueError, UnicodeError) as exc:
            raise ProtocolError("Grove did not return a JSON API response. Check its URL.") from exc
        if not isinstance(data, dict):
            raise ProtocolError("Grove returned an unexpected API response.")
        return data

    def _request(
        self, method: str, endpoint: str, token: str | None = None, *, timeout: float | None = None
    ) -> dict:
        headers = self._headers(token)
        connection = None
        try:
            connection = self._connection(timeout)
            connection.request(method, self._path(endpoint), headers=headers)
            return self._response(connection)
        except PermissionError as exc:
            raise HostPermissionRequired() from exc
        except (OSError, http.client.HTTPException) as exc:
            raise GroveError("Could not reach Grove. Check its URL and connection.") from exc
        finally:
            if connection is not None:
                connection.close()

    def require_user_auth(self, *, timeout: float | None = None) -> None:
        if self._request("GET", "/auth/status", timeout=timeout).get("auth_enabled") is not True:
            raise AuthenticationRequired("Enable user authentication in Grove before connecting this plugin.")

    def account(self, token: str) -> Account:
        return Account.from_response(self._request("GET", "/auth/me", token))

    def logout(self, token: str) -> None:
        self._request("POST", "/auth/logout", token)

    def upload(
        self,
        job: SlicedJob,
        token: str,
        progress: ProgressCallback = lambda _: None,
        cancelled: CancelCallback = lambda: False,
    ) -> int:
        job = job.validate()
        headers = self._headers(token)
        boundary = "grove-" + uuid4().hex
        filename = job.filename or job.path.name
        prefix = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8")
        suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
        connection = None
        try:
            connection = self._connection()
            with job.path.open("rb") as source:
                initial = os.fstat(source.fileno())
                headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
                headers["Content-Length"] = str(len(prefix) + initial.st_size + len(suffix))
                if cancelled():
                    raise Cancelled("Upload cancelled.")
                connection.putrequest("POST", self._path("/library/files"))
                for name, value in headers.items():
                    connection.putheader(name, value)
                connection.endheaders()
                connection.send(prefix)
                sent = 0
                progress(Progress("uploading", 0, initial.st_size))
                while sent < initial.st_size:
                    if cancelled():
                        raise Cancelled("Upload cancelled.")
                    chunk = source.read(min(256 * 1024, initial.st_size - sent))
                    if not chunk:
                        raise GroveError("The exported job changed during upload. Export it again.")
                    connection.send(chunk)
                    sent += len(chunk)
                    progress(Progress("uploading", sent, initial.st_size))
                final = os.fstat(source.fileno())
                if (final.st_size, final.st_mtime_ns) != (initial.st_size, initial.st_mtime_ns):
                    raise GroveError("The exported job changed during upload. Export it again.")
                if cancelled():
                    raise Cancelled("Upload cancelled.")
                connection.send(suffix)
                data = self._response(connection)
            return positive_id(data.get("id"), "library file ID")
        except PermissionError as exc:
            raise HostPermissionRequired("access Grove or read the sliced archive") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise GroveError(
                "Upload was interrupted. A library file may remain in Grove; no queue request was made."
            ) from exc
        finally:
            if connection is not None:
                connection.close()
