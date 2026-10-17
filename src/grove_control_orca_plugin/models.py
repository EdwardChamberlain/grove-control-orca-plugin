"""Values shared by the HTTP client, Grove bridge and Orca adapter."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urlunsplit


class GroveError(Exception):
    """An error whose message is safe to display without exposing credentials."""


class AuthenticationRequired(GroveError):
    pass


class HostPermissionRequired(GroveError):
    """Orca denied a host permission; this is separate from Grove authentication."""

    def __init__(self, resource: str = "connect to Grove"):
        super().__init__(
            f"Orca blocked permission to {resource}. Approve Orca's plugin permission request, "
            "then use Check connection before sending again. If the Send dialog is open, choose Cancel."
        )


class Cancelled(GroveError):
    pass


class ProtocolError(GroveError):
    pass


class HttpError(GroveError):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"Grove returned HTTP {status}.")


def normalize_url(value: str) -> str:
    if not isinstance(value, str):
        raise GroveError("Enter Grove's full HTTP or HTTPS URL.")
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError as exc:
        raise GroveError("Enter a valid Grove URL and port.") from exc
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise GroveError("Enter Grove's full HTTP or HTTPS URL.")
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise GroveError("The Grove URL must not contain credentials, a query or a fragment.")
    if any(ord(char) < 32 or char == "\\" for char in value) or any(c.isspace() for c in parts.netloc):
        raise GroveError("Enter a valid Grove URL.")
    hostname = parts.hostname
    host = f"[{hostname}]" if ":" in hostname else hostname.encode("idna").decode("ascii")
    netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))


def positive_id(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ProtocolError(f"Grove returned an invalid {label}.")
    return value


@dataclass(frozen=True)
class Account:
    id: int
    username: str

    @classmethod
    def from_response(cls, data: dict) -> Account:
        user_id = positive_id(data.get("id"), "user ID")
        username = data.get("username")
        if not isinstance(username, str) or not username or data.get("is_active") is False:
            raise AuthenticationRequired("Sign in with an active Grove user account.")
        return cls(user_id, username)


@dataclass(frozen=True)
class Session:
    grove_url: str
    account: Account
    token: str = field(repr=False)


@dataclass(frozen=True)
class SlicedJob:
    """An exported artifact; its path must remain readable until submission returns."""

    path: Path
    filename: str | None = None
    plate_id: int | None = None

    @classmethod
    def from_native(
        cls, path: Path, project_name: str | None = None, plate_id: int | None = None
    ) -> SlicedJob:
        """Separate Orca's temporary archive path from its Grove upload name.

        Native Send uses hidden .3mf files, unlike the .gcode.3mf names used by
        manual exports. This does not rename or trust the file: validate() still
        checks its G-code, metadata, plate selection, and ZIP integrity.
        """
        path = Path(path)
        if path.suffix.lower() != ".3mf":
            raise GroveError(
                "Orca did not provide a 3MF archive. Enable Use 3MF instead of G-code in the printer preset."
            )
        name = project_name if isinstance(project_name, str) and project_name.strip() else ""
        if not name:
            name = path.name if not path.name.startswith(".") else f"Orca plate {plate_id or 1}"
        name = re.sub(r'[\x00-\x1f\x7f/\\:"<>|?*]', "_", name).strip(" .")
        for suffix in (".gcode.3mf", ".3mf", ".gcode"):
            if name.lower().endswith(suffix):
                name = name[: -len(suffix)]
                break
        name = name[:160].strip(" .") or f"Orca plate {plate_id or 1}"
        return cls(path, f"{name}.gcode.3mf", plate_id)

    def validate(self) -> SlicedJob:
        from .artifact import validate_sliced_archive

        path = Path(self.path)
        filename = self.filename or path.name
        if not filename.lower().endswith(".gcode.3mf"):
            raise GroveError("Orca did not provide a sliced .gcode.3mf archive.")
        if Path(filename).name != filename or any(c in filename for c in ("\\", "\r", "\n", '"')):
            raise GroveError("The exported job has an invalid filename.")
        try:
            readable = path.is_file() and path.stat().st_size > 0
        except PermissionError as exc:
            raise HostPermissionRequired("read the sliced archive") from exc
        except OSError:
            readable = False
        if not readable:
            raise GroveError("The exported sliced job is missing or empty.")
        plate_ids = validate_sliced_archive(path, verify_crc=True)
        if self.plate_id is not None:
            positive_id(self.plate_id, "plate ID")
            if self.plate_id not in plate_ids:
                raise GroveError("The selected Orca plate is not present in the sliced archive.")
        return SlicedJob(path, filename, self.plate_id)


@dataclass(frozen=True)
class Progress:
    phase: str
    completed_bytes: int = 0
    total_bytes: int = 0


class SubmissionStatus(str, Enum):
    SUBMITTED = "submitted"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SubmissionResult:
    status: SubmissionStatus
    message: str
    library_file_id: int | None = None
    queue_item_ids: tuple[int, ...] = ()


ProgressCallback = Callable[[Progress], None]
CancelCallback = Callable[[], bool]
