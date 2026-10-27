"""The only environment constructor; rules and dependency are checked at runtime."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

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


def new_environment(seed: int):
    require_engine()
    from riichienv import GameRule, RiichiEnv

    return RiichiEnv(seed=seed, game_mode=Rules().game_mode, rule=GameRule.default_tenhou())
