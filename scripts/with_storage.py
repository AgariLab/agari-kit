#!/usr/bin/env python3
"""Bootstrap without third-party imports; cache variables are set before running uv."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agari_kit.storage import Storage, StorageError


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/with_storage.py COMMAND [ARG ...]", file=sys.stderr)
        return 2
    try:
        storage = Storage.from_env()
        storage.initialize()
        env = dict(os.environ, **storage.environment())
        os.execvpe(sys.argv[1], sys.argv[1:], env)
    except (StorageError, OSError) as exc:
        print(f"Bootstrap failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
