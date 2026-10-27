import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agari_kit.storage import Storage, StorageError


class StorageTests(unittest.TestCase):
    def test_unmounted_volume_is_never_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = Path(tmp) / "missing-volume"
            storage = Storage(mount / "AgariLab", mount)
            with self.assertRaisesRegex(StorageError, "not mounted"):
                storage.initialize(min_free_bytes=0)
            self.assertFalse(mount.exists())

    def test_explicit_root_with_spaces_and_chinese_and_project_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "新加卷 project"
            storage = Storage.from_env({"AGARILAB_STORAGE_ROOT": str(root)})
            report = storage.initialize(min_free_bytes=0)
            self.assertTrue(report["write_verified"])
            env = storage.environment()
            for key in ("UV_CACHE_DIR", "HF_HOME", "HF_HUB_CACHE", "TORCH_HOME", "TMPDIR"):
                self.assertTrue(Path(env[key]).is_dir())
                self.assertIn(root, Path(env[key]).parents)

    def test_short_space_prevents_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "new")
            with self.assertRaisesRegex(StorageError, "Insufficient"):
                storage.initialize(min_free_bytes=2**100)
            self.assertFalse(storage.root.exists())

    def test_write_denial_has_actionable_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "new")
            with patch("agari_kit.storage.tempfile.TemporaryFile", side_effect=PermissionError(1, "denied")):
                with self.assertRaisesRegex(StorageError, "not writable"):
                    storage.initialize(min_free_bytes=0)
            self.assertFalse(storage.root.exists())

    def test_path_traversal_and_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            (root / "cache").symlink_to(tmp, target_is_directory=True)
            storage = Storage(root)
            for path in ("../escape", "/escape", "cache/uv"):
                with self.assertRaises(StorageError):
                    storage.path(path)
            with self.assertRaises(StorageError):
                storage.initialize(min_free_bytes=0)
            self.assertFalse((root / "datasets").exists())

    def test_volume_override_still_requires_mount(self):
        storage = Storage.from_env({"AGARILAB_STORAGE_ROOT": "/Volumes/missing-agari-volume/data"})
        with self.assertRaisesRegex(StorageError, "not mounted"):
            storage.check(min_free_bytes=0)

    def test_volume_mount_cannot_be_disabled_by_broader_mount(self):
        with self.assertRaises(StorageError):
            Storage.from_env({"AGARILAB_STORAGE_ROOT": "/Volumes/missing-agari/data",
                              "AGARILAB_STORAGE_MOUNT": "/"})

    def test_macos_lowercase_volumes_still_checks_mount(self):
        with patch("agari_kit.storage.sys.platform", "darwin"):
            storage = Storage.from_env({"AGARILAB_STORAGE_ROOT": "/volumes/missing-agari/data"})
        self.assertEqual(storage.mount, Path("/Volumes/missing-agari"))
        with self.assertRaisesRegex(StorageError, "not mounted"):
            storage.check(min_free_bytes=0)

    def test_read_only_inspection_does_not_claim_write_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Storage(Path(tmp) / "new").check(min_free_bytes=0)
            self.assertFalse(report["write_verified"])
            self.assertFalse((Path(tmp) / "new").exists())


if __name__ == "__main__":
    unittest.main()
