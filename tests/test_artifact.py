import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from grove_control_orca_plugin.artifact import validate_sliced_archive
from grove_control_orca_plugin.models import GroveError, HostPermissionRequired, SlicedJob


def make_sliced_archive(path: Path, *, plate_ids=(1,), extra=b"") -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        if extra:
            archive.writestr("3D/mesh.bin", extra)
        for plate_id in plate_ids:
            prefix = f"Metadata/plate_{plate_id}"
            archive.writestr(f"{prefix}.gcode", b"G28\n")
            archive.writestr(f"{prefix}.gcode.md5", b"d41d8cd98f00b204e9800998ecf8427e")
            archive.writestr(f"{prefix}.json", b"{}")


class ArtifactTests(unittest.TestCase):
    def test_native_temporary_3mf_keeps_source_path_and_validates_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".52871.0.3mf"
            make_sliced_archive(path, plate_ids=(1, 3))
            before = path.read_bytes()
            job = SlicedJob.from_native(path, "3DBenchy", plate_id=3).validate()
            self.assertEqual(job.path, path)
            self.assertEqual(job.filename, "3DBenchy.gcode.3mf")
            self.assertEqual(job.plate_id, 3)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_native_upload_name_is_safe_and_does_not_repeat_sliced_suffix(self):
        path = Path(".52871.0.3mf")
        cases = (
            (None, "Orca plate 3.gcode.3mf"),
            ("", "Orca plate 3.gcode.3mf"),
            ("pièce", "pièce.gcode.3mf"),
            ("3DBenchy.gcode.3mf", "3DBenchy.gcode.3mf"),
            ("3DBenchy.3mf", "3DBenchy.gcode.3mf"),
            ("3DBenchy.gcode", "3DBenchy.gcode.3mf"),
            ('folder/part\\name"\r\n', "folder_part_name___.gcode.3mf"),
            (" ... ", "Orca plate 3.gcode.3mf"),
        )
        for project_name, expected in cases:
            with self.subTest(project_name=project_name):
                self.assertEqual(SlicedJob.from_native(path, project_name, 3).filename, expected)
        self.assertEqual(SlicedJob.from_native(Path("source.gcode.3mf")).filename, "source.gcode.3mf")

    def test_native_naming_does_not_admit_model_only_or_corrupt_archives(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".52871.0.3mf"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("3D/3dmodel.model", b"model")
            with self.assertRaisesRegex(GroveError, "native print callback"):
                SlicedJob.from_native(path, "3DBenchy", 1).validate()
            path.write_bytes(b"not a ZIP archive")
            with self.assertRaisesRegex(GroveError, "not a complete, readable"):
                SlicedJob.from_native(path, "3DBenchy", 1).validate()

    def test_native_naming_rejects_raw_gcode_and_unknown_source_formats(self):
        for filename in ("part.gcode", "part.stl", "part.tmp"):
            with self.subTest(filename=filename), self.assertRaisesRegex(GroveError, "Use 3MF"):
                SlicedJob.from_native(Path(filename), "part", 1)

    def test_denied_archive_read_is_not_misreported_as_corrupt(self):
        with patch("zipfile.ZipFile", side_effect=PermissionError("private-archive-path")):
            with self.assertRaises(HostPermissionRequired) as raised:
                validate_sliced_archive(Path("part.gcode.3mf"))
        self.assertIn("read the sliced archive", str(raised.exception))
        self.assertNotIn("private-archive-path", str(raised.exception))

    def test_validates_native_archive_and_returns_one_based_plates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.gcode.3mf"
            make_sliced_archive(path, plate_ids=(1, 3), extra=bytes(range(256)) * 2500)
            self.assertEqual(validate_sliced_archive(path, verify_crc=True), (1, 3))
            job = SlicedJob(path, plate_id=3).validate()
            self.assertEqual(job.plate_id, 3)

    def test_rejects_raw_gcode_and_model_only_3mf(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "part.gcode"
            raw.write_text("G28\n")
            with self.assertRaisesRegex(GroveError, "sliced .gcode.3mf"):
                SlicedJob(raw).validate()

            model_only = root / "project.gcode.3mf"
            with zipfile.ZipFile(model_only, "w") as archive:
                archive.writestr("3D/3dmodel.model", b"model")
            with self.assertRaisesRegex(GroveError, "native print callback"):
                validate_sliced_archive(model_only)

    def test_rejects_missing_metadata_and_a_plate_not_in_the_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            incomplete = root / "incomplete.gcode.3mf"
            with zipfile.ZipFile(incomplete, "w") as archive:
                archive.writestr("Metadata/plate_1.gcode", b"G28\n")
            with self.assertRaisesRegex(GroveError, "missing G-code or metadata"):
                validate_sliced_archive(incomplete)

            complete = root / "plate.gcode.3mf"
            make_sliced_archive(complete, plate_ids=(1,))
            with self.assertRaisesRegex(GroveError, "not present"):
                SlicedJob(complete, plate_id=2).validate()


if __name__ == "__main__":
    unittest.main()
