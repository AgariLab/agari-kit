"""The only environment constructor; rules and dependency are checked at runtime."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from functools import lru_cache
from pathlib import Path
import hashlib

from .agent import Rules


def require_engine() -> str:
    expected = Rules().engine_version
    try:
        actual = version("riichienv")
    except PackageNotFoundError as exc:
        raise RuntimeError("RiichiEnv is not installed; bootstrap Python 3.12 and uv sync first") from exc
    if actual != expected:
        raise RuntimeError(f"RiichiEnv mismatch: expected {expected}, installed {actual}")
    return actual


@lru_cache(maxsize=4)
def _native_digest(path: str, modified_ns: int, size: int) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def engine_identity() -> dict:
    """Distinguish upstream and fork builds even when their version is identical."""
    installed = require_engine()
    from riichienv import _riichienv
    path = Path(_riichienv.__file__).resolve()
    stat = path.stat()
    return {"version": installed,
            "binary_sha256": _native_digest(str(path), stat.st_mtime_ns, stat.st_size)}


def new_environment(seed: int):
    require_engine()
    from riichienv import GameRule, RiichiEnv

    return RiichiEnv(seed=seed, game_mode=Rules().game_mode, rule=GameRule.default_tenhou())
