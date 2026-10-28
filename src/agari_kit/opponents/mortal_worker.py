"""Original JSONL RPC worker; imports the user's external Mortal installation.

Run as a script with the isolated runtime interpreter, not the kit interpreter.
"""
from __future__ import annotations
import argparse
import contextlib
import importlib.util
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--threads", type=int, required=True)
    args = parser.parse_args()
    output = sys.stdout
    with contextlib.redirect_stdout(sys.stderr):
        import torch
        torch.set_num_threads(args.threads)
        torch.set_num_interop_threads(1)
        spec = importlib.util.spec_from_file_location("agari_external_mortal", args.checkout / "inference.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        engine = module.load_engine("cpu")
    def emit(value):
        print(json.dumps(value, allow_nan=False, separators=(",", ":")), file=output, flush=True)
    emit({"ready": True, "protocol": "agari-mortal-worker-v1"})
    session = None
    previous = 0
    while True:
        line = sys.stdin.buffer.readline(2**20 + 1)
        if not line:
            return 0
        request = {}
        try:
            if len(line) > 2**20 or not line.endswith(b"\n"):
                raise ValueError("Invalid request size")
            request = json.loads(line)
            if type(request.get("id")) is not int or request["id"] != previous + 1:
                raise ValueError("Nonconsecutive request id")
            previous = request["id"]
            with contextlib.redirect_stdout(sys.stderr):
                if request["command"] == "reset" and set(request) == {"id", "command", "seat"}:
                    session = module.Session(engine, request["seat"])
                    response = {"id": previous, "reset": True}
                elif request["command"] == "act" and set(request) == {"id", "command", "events"}:
                    if session is None or not isinstance(request["events"], list) or not request["events"]:
                        raise ValueError("Missing session or events")
                    action = None
                    for event in request["events"]:
                        reaction = session.react(event)
                        if reaction is not None:
                            if action is not None:
                                raise ValueError("Multiple reactions to one decision request")
                            action = reaction
                    response = {"id": previous, "action": action}
                else:
                    raise ValueError("Unknown command or extra fields")
            emit(response)
        except Exception as exc:
            emit({"id": request.get("id"), "error": type(exc).__name__ + ": " + str(exc)[:500]})
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
