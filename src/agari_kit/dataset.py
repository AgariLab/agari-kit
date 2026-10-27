"""Whole-game metadata registration and immutable chronological split manifests."""

from __future__ import annotations

import hashlib
import json
import fcntl
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .mjai import DataError, canonical_json


def utc_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise DataError("played_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise DataError("played_at needs a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


class Catalog:
    def __init__(self, path: Path):
        self.connection = sqlite3.connect(path)
        self.connection.execute("""CREATE TABLE IF NOT EXISTS games (
            game_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE,
            played_at TEXT NOT NULL, source TEXT NOT NULL, rules TEXT NOT NULL,
            usage_basis TEXT NOT NULL, validation TEXT NOT NULL)""")
        self.connection.commit()
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(games)")}
        for name in ("mjai_path", "trace_path", "validation_receipt", "validation_receipt_sha256"):
            if name not in columns:
                self.connection.execute(f"ALTER TABLE games ADD COLUMN {name} TEXT")
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def register(self, *, game_id: str, sha256: str, played_at: str, source: str,
                 rules: str, usage_basis: str, validation: str, mjai_path: str | None = None,
                 trace_path: str | None = None, validation_receipt: str | None = None,
                 validation_receipt_sha256: str | None = None) -> bool:
        if not all(isinstance(value, str) and value.strip() for value in
                   (game_id, sha256, source, rules, usage_basis, validation)):
            raise DataError("Registration metadata cannot be empty")
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            raise DataError("Expected lowercase SHA-256")
        if rules != "4p-red-half/tenhou":
            raise DataError("Unsupported rule group")
        row = (game_id, sha256, utc_timestamp(played_at), source, rules, usage_basis, validation,
               mjai_path, trace_path, validation_receipt, validation_receipt_sha256)
        existing = self.connection.execute(
            "SELECT * FROM games WHERE game_id=? OR sha256=?", (game_id, sha256)
        ).fetchall()
        if existing:
            if existing == [row]:
                return False
            # A duplicate with conflicting identity, timestamps or provenance
            # requires an explicit review, never a silent overwrite.
            raise DataError("Duplicate game or hash with conflicting metadata")
        with self.connection:
            self.connection.execute("INSERT INTO games VALUES (?,?,?,?,?,?,?,?,?,?,?)", row)
        return True

    def games(self) -> list[dict]:
        cursor = self.connection.execute("SELECT * FROM games ORDER BY played_at, game_id")
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor]


def make_split(games: list[dict]) -> dict:
    if len(games) < 20:
        raise DataError("At least 20 complete games are needed for nonempty 90/5/5 splits")
    if any(game["validation"] != "engine-verified" for game in games):
        raise DataError("Training splits require engine-verified games, not structural inspection")
    ids = [game["game_id"] for game in games]
    hashes = [game["sha256"] for game in games]
    if len(set(ids)) != len(ids) or len(set(hashes)) != len(hashes):
        raise DataError("Duplicate complete games in split input")
    if len({game["rules"] for game in games}) != 1 or len({game["source"] for game in games}) != 1:
        raise DataError("Split one source/rule group at a time")
    ordered = sorted(games, key=lambda game: (utc_timestamp(game["played_at"]), game["game_id"]))
    train_end, val_end = len(ordered) * 90 // 100, len(ordered) * 95 // 100
    partitions = {"train": ordered[:train_end], "validation": ordered[train_end:val_end], "test": ordered[val_end:]}
    result = {"schema_version": 1, "policy": "whole-game-time-90-5-5", "partitions": partitions}
    result["dataset_sha256"] = hashlib.sha256(canonical_json(result).encode()).hexdigest()
    return result


def freeze_split(path: Path, manifest: dict) -> bool:
    serialized = canonical_json(manifest) + "\n"
    # ExFAT cannot hardlink a prepared file into place. A process-released lock
    # plus same-directory atomic rename protects cooperative writers and avoids
    # publishing partial manifests when writing or fsync fails.
    with path.with_name(path.name + ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DataError("Another process is freezing this split; retry after it completes") from exc
        if path.exists():
            if path.read_text(encoding="utf-8") != serialized:
                raise DataError("Frozen split differs; select a new explicit dataset version")
            return False
        fd, temporary = tempfile.mkstemp(prefix=".agari-split-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            return True
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
