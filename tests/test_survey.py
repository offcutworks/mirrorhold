"""The source survey and the built-in entry classifications it applies.

The survey reads directory listings only: it never opens a file (a placeholder would
download), never follows a link, and leaves out a drive-root source's operating
system artefacts."""
import ctypes
import os
import shutil
import sys
import unittest

from support import TempDirTest

from mirrorhold.jobs.exclusions import is_cloud_placeholder, is_drive_root_artefact, is_link
from mirrorhold.jobs.survey import SourceSurvey, describe, survey_source

WINDOWS = sys.platform == "win32"


def set_attributes(path, attributes):
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.SetFileAttributesW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    if not k32.SetFileAttributesW(str(path), attributes):
        raise ctypes.WinError(ctypes.get_last_error())


class ClassificationTest(unittest.TestCase):
    def test_drive_root_artefacts_need_both_hidden_and_system(self):
        self.assertTrue(is_drive_root_artefact(0x2 | 0x4 | 0x20))
        self.assertFalse(is_drive_root_artefact(0x2))
        self.assertFalse(is_drive_root_artefact(0x4))

    def test_placeholders(self):
        self.assertTrue(is_cloud_placeholder(0x400000))
        self.assertTrue(is_cloud_placeholder(0x40000))
        self.assertTrue(is_cloud_placeholder(0x1000))
        self.assertFalse(is_cloud_placeholder(0x20 | 0x400))

    def test_links_are_name_surrogate_reparse_points_only(self):
        self.assertTrue(is_link(0x400, 0xA000000C))   # symbolic link
        self.assertTrue(is_link(0x400, 0xA0000003))   # junction
        self.assertFalse(is_link(0x400, 0x9000101A))  # a cloud file's reparse point
        self.assertFalse(is_link(0x20, 0))


class SurveyTest(TempDirTest):
    def make(self, rel, size):
        path = self.tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        return path

    def test_counts_sizes_and_largest(self):
        self.make("a.bin", 10)
        self.make("sub/b.bin", 300)
        self.make("sub/deeper/c.bin", 20)
        survey = survey_source(self.tmp, drive_root=False)
        self.assertEqual((survey.files, survey.total_bytes, survey.largest_file_bytes), (3, 330, 300))
        self.assertTrue(survey.complete)
        self.assertIn("3 files", describe(survey))
        self.assertEqual(describe(None), "not surveyed")

    @unittest.skipUnless(WINDOWS, "attributes and junctions are Windows-only")
    def test_drive_root_artefacts_are_skipped_only_at_a_drive_root(self):
        self.make("data.bin", 5)
        artefact = self.make("pagefile.sys", 1000)
        set_attributes(artefact, 0x2 | 0x4)
        nested = self.make("sub/hidden-system.bin", 7)
        set_attributes(nested, 0x2 | 0x4)
        try:
            as_root = survey_source(self.tmp, drive_root=True)
            as_folder = survey_source(self.tmp, drive_root=False)
        finally:
            set_attributes(artefact, 0x80)
            set_attributes(nested, 0x80)
        self.assertEqual((as_root.files, as_root.total_bytes), (2, 12), "only the root-level artefact is left out")
        self.assertEqual((as_folder.files, as_folder.total_bytes), (3, 1012))

    @unittest.skipUnless(WINDOWS, "junctions are Windows-only")
    def test_links_are_not_followed(self):
        import subprocess
        self.make("real/file.bin", 50)
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(self.tmp / "junction"), str(self.tmp / "real")],
                                capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, "the junction could not be created")
        survey = survey_source(self.tmp, drive_root=False)
        self.assertEqual((survey.files, survey.links_skipped), (1, 1))

    @unittest.skipUnless(WINDOWS, "attributes are Windows-only")
    def test_offline_files_are_counted_apart(self):
        self.make("local.bin", 5)
        cloud = self.make("cloud.docx", 4000)
        set_attributes(cloud, 0x1000)
        try:
            survey = survey_source(self.tmp, drive_root=False)
        finally:
            set_attributes(cloud, 0x80)
        self.assertEqual((survey.files, survey.total_bytes, survey.placeholders), (1, 5, 1))

    def test_an_unlistable_folder_makes_the_survey_incomplete(self):
        missing = self.tmp / "gone"
        survey = survey_source(missing, drive_root=False)
        self.assertEqual(survey, SourceSurvey(unreadable_directories=1))
        self.assertFalse(survey.complete)

    @unittest.skipUnless(WINDOWS, "Windows names")
    def test_folders_whose_names_end_in_a_space_or_a_period_are_walked(self):
        long = "\\\\?\\"
        self.addCleanup(shutil.rmtree, long + str(self.tmp), ignore_errors=True)
        for name in ("Album ", "Dots."):
            os.makedirs(long + str(self.tmp / name / "inner."))
            for folder in (self.tmp / name, self.tmp / name / "inner."):
                with open(long + str(folder / "photo.jpg"), "wb") as handle:
                    handle.write(b"x" * 10)
        survey = survey_source(self.tmp, drive_root=False)
        self.assertEqual((survey.files, survey.total_bytes, survey.unreadable_directories), (4, 40, 0))


class SurveyWordsTest(unittest.TestCase):
    """The survey line counts each figure in the number it needs: one file, one byte, one
    folder that could not be listed; two or more of them in the plural."""

    def test_one_of_each_is_singular_and_two_are_plural(self):
        one = SourceSurvey(files=1, total_bytes=1, largest_file_bytes=1, placeholders=1, unreadable_directories=1)
        self.assertEqual(describe(one), "1 file, 1 byte, largest 1 byte, 1 cloud-only placeholder not counted, "
                                        "1 folder could not be listed")
        two = SourceSurvey(files=2, total_bytes=2, largest_file_bytes=2, placeholders=2, unreadable_directories=2)
        self.assertEqual(describe(two), "2 files, 2 bytes, largest 2 bytes, 2 cloud-only placeholders not counted, "
                                        "2 folders could not be listed")


if __name__ == "__main__":
    unittest.main()
