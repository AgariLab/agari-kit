"""Project-scoped storage configuration, usable before dependencies are installed."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

DEFAULT_ROOT = Path("/Volumes/新加卷/AgariLab")
DIRECTORIES = (
    "datasets/raw", "datasets/mjai", "datasets/features", "datasets/manifests",
    "datasets/quarantine", "models", "checkpoints", "runs", "cache/uv",
    "cache/huggingface/hub", "cache/torch", "tmp",
)


class StorageError(RuntimeError):
    pass


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


@dataclass(frozen=True)
class Storage:
    root: Path
    mount: Path | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Storage:
        env = os.environ if environ is None else environ
        root = Path(env.get("AGARILAB_STORAGE_ROOT", str(DEFAULT_ROOT))).expanduser()
        if not root.is_absolute() or ".." in root.parts:
            raise StorageError("AGARILAB_STORAGE_ROOT must be an absolute path without '..'")
        if sys.platform == "darwin" and len(root.parts) >= 3 and root.parts[1].lower() == "volumes":
            root = Path("/Volumes", *root.parts[2:])
        configured_mount = env.get("AGARILAB_STORAGE_MOUNT")
        # /Volumes/name must remain a real mount even when the root is overridden.
        inferred = Path(*root.parts[:3]) if root.parts[:2] == ("/", "Volumes") and len(root.parts) >= 3 else None
        mount = Path(configured_mount).expanduser() if configured_mount else inferred
        if mount is not None:
            if not mount.is_absolute() or not _inside(root, mount):
                raise StorageError("Storage root must be inside AGARILAB_STORAGE_MOUNT")
            if inferred is not None and mount != inferred:
                raise StorageError("A /Volumes storage root must use its own volume mount")
        return cls(root=root, mount=mount)

    def _check_mount(self) -> None:
        if self.mount is not None:
            if self.mount.is_symlink() or not self.mount.is_mount():
                raise StorageError(f"Storage volume is not mounted: {self.mount}")
            if not _inside(self.root.resolve(), self.mount.resolve()):
                raise StorageError("Storage root resolves outside the configured volume")

    def path(self, relative: str) -> Path:
        self._check_mount()
        candidate = self.root / relative
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise StorageError("Storage paths must be relative and cannot traverse parents")
        if not _inside(candidate.resolve(), self.root.resolve()):
            raise StorageError(f"Storage path escapes root via symlink: {candidate}")
        return candidate

    def check(self, *, min_free_bytes: int = 5 * 1024**3, probe: bool = False) -> dict:
        if min_free_bytes < 0:
            raise StorageError("Minimum free space cannot be negative")
        self._check_mount()
        ancestor = self.root
        while not ancestor.exists() and ancestor != ancestor.parent:
            ancestor = ancestor.parent
        if not ancestor.is_dir():
            raise StorageError(f"Storage ancestor is not a directory: {ancestor}")
        # Explicit overrides must have an existing parent, preventing typos from
        # silently creating an entirely different storage hierarchy.
        if self.mount is None and not self.root.parent.is_dir():
            raise StorageError(f"Storage parent does not exist: {self.root.parent}")
        try:
            # Existing subdirectories can have different permissions or be on
            # nested mounts, so validating only the root's parent is insufficient.
            for name in DIRECTORIES:
                path = self.path(name)
                if path.exists():
                    if not path.is_dir():
                        raise StorageError(f"Storage destination is not a directory: {path}")
                    if shutil.disk_usage(path).free < min_free_bytes:
                        raise StorageError(f"Insufficient space at {path}")
            free = shutil.disk_usage(ancestor).free
            if free < min_free_bytes:
                raise StorageError(f"Insufficient space at {ancestor}: {free} < {min_free_bytes} bytes")
            if probe:
                with tempfile.TemporaryFile(prefix=".agari-write-probe-", dir=ancestor) as stream:
                    stream.write(b"agari-storage-probe")
                    stream.flush()
                    os.fsync(stream.fileno())
        except OSError as exc:
            raise StorageError(f"Storage is not writable at {ancestor}: {exc.strerror}") from exc
        return {"root": str(self.root), "mount": str(self.mount) if self.mount else None,
                "free_bytes": free, "write_verified": probe}

    def initialize(self, *, min_free_bytes: int = 5 * 1024**3) -> dict:
        report = self.check(min_free_bytes=min_free_bytes, probe=True)
        # Prevalidate every destination before creating any directory.
        targets = [self.path(name) for name in DIRECTORIES]
        try:
            for path in targets:
                path.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryFile(prefix=".agari-write-probe-", dir=path) as stream:
                    stream.write(b"probe")
                    stream.flush()
                    os.fsync(stream.fileno())
        except OSError as exc:
            raise StorageError(f"Cannot initialize storage: {exc}") from exc
        return report

    def environment(self) -> dict[str, str]:
        return {
            "AGARILAB_STORAGE_ROOT": str(self.root),
            "UV_CACHE_DIR": str(self.path("cache/uv")),
            "UV_LINK_MODE": "copy",
            "HF_HOME": str(self.path("cache/huggingface")),
            "HF_HUB_CACHE": str(self.path("cache/huggingface/hub")),
            "TORCH_HOME": str(self.path("cache/torch")),
            "TMPDIR": str(self.path("tmp")),
        }
