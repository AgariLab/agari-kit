"""Identify the actual shared-tool source, including uncommitted edits."""

import hashlib
from pathlib import Path

from . import __version__


def source_fingerprint() -> dict:
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for source in sorted(root.rglob("*.py")):
        digest.update(source.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(source.read_bytes())
        digest.update(b"\0")
    return {"package": "agari-kit", "version": __version__, "source_sha256": digest.hexdigest()}
