from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from .evaluation import evaluate
from .mjai import inspect_game, read_events
from .storage import Storage


def load_token(path: Path | None) -> str:
    if path is None:
        token = os.environ.get("RIICHI_BOT_TOKEN", "")
    else:
        try:
            token = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise ValueError("Cannot read the local Bot Token file") from None
    if not token or any(character.isspace() for character in token):
        raise ValueError("Set a valid RIICHI_BOT_TOKEN or provide --token-file")
    return token


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="agari", description="AgariLab shared research tools")
    root.add_argument("--version", action="version", version="agari-kit 0.1.0")
    commands = root.add_subparsers(dest="command", required=True)
    storage = commands.add_parser("storage", help="Inspect or initialize artifact storage")
    storage.add_argument("action", choices=("check", "init", "env"))
    storage.add_argument("--probe", action="store_true", help="Test writes during check")
    storage.add_argument("--min-free-gib", type=float, default=5.0)
    data = commands.add_parser("data", help="Inspect games or freeze a verified catalog")
    data_commands = data.add_subparsers(dest="data_command", required=True)
    inspect = data_commands.add_parser("inspect", help="Structural check only; NOT engine validation")
    inspect.add_argument("path", type=Path)
    validate = data_commands.add_parser("validate", help="Validate a replay; full verification requires its seeded trace")
    validate.add_argument("path", type=Path)
    validate.add_argument("--trace", type=Path)
    selfplay = data_commands.add_parser("selfplay", help="Acquire and verify complete self-play games on artifact storage")
    selfplay.add_argument("--name", required=True)
    selfplay.add_argument("--games", type=int, default=1000)
    selfplay.add_argument("--seed-start", type=int, default=0)
    selfplay.add_argument("--agents", nargs=4, default=["efficiency"] * 4,
                          help="Four builtin names or @agent-descriptor paths")
    split = data_commands.add_parser("split")
    split.add_argument("catalog", help="Relative path below datasets/manifests")
    split.add_argument("output", help="Relative frozen manifest below datasets/manifests")
    evaluation = commands.add_parser("evaluate", help="Run complete four-seat seed groups")
    evaluation.add_argument("--candidate", default="random", help="Builtin name or @agent-descriptor.json")
    evaluation.add_argument("--opponents", nargs=3, default=["tsumogiri"] * 3)
    evaluation.add_argument("--seed-start", type=int, default=0)
    evaluation.add_argument("--seed-count", type=int, default=1, help="Four hanchan per seed")
    evaluation.add_argument("--run", required=True, help="New relative directory below runs")
    replay = commands.add_parser("replay", help="Export a self-contained offline MJAI replay HTML")
    replay.add_argument("path", type=Path)
    replay.add_argument("--output", type=Path, required=True, help="New .html artifact (never overwritten)")
    replay.add_argument("--player", type=int, choices=range(4),
                        help="Export only this player's visible events; default: full-information research")
    connect = commands.add_parser("connect", help="Connect an agent to riichi.dev")
    connect.add_argument("mode", choices=("validate", "ranked"))
    connect.add_argument("--agent", default="tsumogiri", help="Builtin name or @agent-descriptor.json")
    connect.add_argument("--games", type=int, default=1)
    connect.add_argument("--run", required=True, help="New relative directory below runs")
    connect.add_argument("--reserve-ms", type=int, default=100)
    connect.add_argument("--idle-timeout", type=float, default=300)
    connect.add_argument("--direct", action="store_true", help="Disable environment proxy for this connection")
    connect.add_argument("--token-file", type=Path, help="Local file containing only the Bot Token; never logged")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "storage":
            storage = Storage.from_env()
            min_free = int(args.min_free_gib * 1024**3)
            if args.action == "env":
                storage.check(min_free_bytes=min_free)
                result = storage.environment()
            elif args.action == "init":
                result = storage.initialize(min_free_bytes=min_free)
            else:
                result = storage.check(min_free_bytes=min_free, probe=args.probe)
        elif args.command == "replay":
            from .replay import export_replay
            result = export_replay(args.path, args.output, player=args.player)
        elif args.command == "data" and args.data_command == "inspect":
            result = inspect_game(read_events(args.path))
        elif args.command == "data" and args.data_command == "validate":
            from .validation import validate_mjai
            result = validate_mjai(args.path, trace_path=args.trace)
        elif args.command == "data":
            from .collection import collect_selfplay, verified_split
            storage = Storage.from_env()
            storage.check(probe=True)
            if args.data_command == "selfplay":
                result = collect_selfplay(storage, name=args.name, games=args.games,
                                          seed_start=args.seed_start, agents=args.agents,
                                          progress=lambda event: print(json.dumps(event), file=sys.stderr, flush=True))
            else:
                catalog = storage.path("datasets/manifests/" + args.catalog)
                if not catalog.is_file():
                    raise ValueError(f"Catalog not found: {catalog}")
                output = storage.path("datasets/manifests/" + args.output)
                result = verified_split(storage, catalog, output)
        elif args.command == "connect":
            from .connect import connect_games
            token = load_token(args.token_file)
            storage = Storage.from_env()
            storage.check(probe=True)
            result = asyncio.run(connect_games(mode=args.mode, agent_name=args.agent, token=token,
                                 games=args.games, output=storage.path("runs/" + args.run),
                                 reserve_ms=args.reserve_ms, idle_timeout=args.idle_timeout,
                                 proxy=None if args.direct else True))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["games_completed"] == args.games and result["validation_passed"] is not False else 1
        else:
            if args.seed_count < 1:
                raise ValueError("seed-count must be positive")
            storage = Storage.from_env()
            storage.check(probe=True)
            output = storage.path("runs/" + args.run)
            result = evaluate(list(range(args.seed_start, args.seed_start + args.seed_count)),
                              [args.candidate] + args.opponents, output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"agari: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
