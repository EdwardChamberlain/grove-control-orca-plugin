"""Receive and validate completed Orca sliced-file exports."""

from __future__ import annotations

import re
import zipfile
import zlib
from pathlib import Path

from .models import GroveError, HostPermissionRequired

_PLATE_GCODE = re.compile(r"Metadata/plate_(\d+)\.gcode\Z")


def validate_sliced_archive(path: Path, *, verify_crc: bool = False) -> tuple[int, ...]:
    """Check an Orca sliced archive and return its 1-based plate IDs."""
    try:
        with zipfile.ZipFile(path, "r") as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise GroveError("The sliced export contains duplicate archive entries.")

            info_by_name = {info.filename: info for info in infos}
            plate_ids = sorted(
                int(match.group(1)) for name in names if (match := _PLATE_GCODE.fullmatch(name))
            )
            if not plate_ids:
                raise GroveError("Orca's native print callback did not provide a sliced .gcode.3mf archive.")

            for plate_id in plate_ids:
                prefix = f"Metadata/plate_{plate_id}"
                required = (f"{prefix}.gcode", f"{prefix}.gcode.md5", f"{prefix}.json")
                if any(name not in info_by_name or info_by_name[name].file_size <= 0 for name in required):
                    raise GroveError(f"The sliced export is missing G-code or metadata for plate {plate_id}.")
                for name in required:
                    with archive.open(info_by_name[name]) as member:
                        while chunk := member.read(64 * 1024):
                            if chunk.strip():
                                break
                        else:
                            raise GroveError(
                                f"The sliced export has empty G-code or metadata for plate {plate_id}."
                            )

            if verify_crc:
                corrupt = archive.testzip()
                if corrupt is not None:
                    raise GroveError("The sliced export is corrupt. Export the plate again from Orca.")
            return tuple(plate_ids)
    except GroveError:
        raise
    except PermissionError as exc:
        raise HostPermissionRequired("read the sliced archive") from exc
    except (
        OSError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        RuntimeError,
        ValueError,
        EOFError,
        NotImplementedError,
        zlib.error,
    ) as exc:
        raise GroveError(
            "The file supplied by Orca is not a complete, readable sliced .gcode.3mf archive."
        ) from exc
