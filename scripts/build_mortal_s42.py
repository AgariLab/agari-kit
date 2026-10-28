#!/usr/bin/env python3
"""Verify a fixed HF download, build its encoder, and bind an isolated runtime.

Download the original release separately (hfd supports revision/local-dir).
This script does not install packages or change the project's Python environment.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
import urllib.request

from agari_kit.storage import Storage

REVISION = "524981f21a929e3c3b269f65af79b84163bd02ca"
WEIGHT_SHA = "e5f6955b6f19007fd65315e53291b36994493046bc118b991b959e9dc596d660"
MORTAL_COMMIT = "0cff2b52982be5b1163aa9a62fb01f03ce91e0d2"
WORKSPACE_COMPLETION = {
    "exe-wrapper/Cargo.toml": "7daa556ce47c8c30f7c931694f17eafe4474a38375b6d305551c63be43da7663",
    "exe-wrapper/src/main.rs": "338b3b77f6caa451053e21cddab3607acc7844f81c94277cd32f19e8d42dde7f",
}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True,
                        help="External directory containing source/ and isolated venv/")
    parser.add_argument("--cargo", type=Path, required=True)
    parser.add_argument("--complete-workspace", action="store_true",
                        help="Fetch two files omitted from the release, from its pinned Mortal upstream")
    args = parser.parse_args()
    storage = Storage.from_env()
    storage.check(probe=True)
    bundle = args.bundle.resolve()
    if storage.root.resolve() not in bundle.parents:
        raise ValueError("Keep the entire Mortal bundle on configured artifact storage")
    source = bundle / "source"
    python = bundle / "venv/bin/python"
    if (bundle / "runtime-manifest.json").exists():
        raise ValueError("Refusing to replace an existing runtime identity")
    metadata = json.loads((source / ".hfd/repo_metadata.json").read_text())
    if metadata.get("sha") != REVISION or metadata.get("id") != "haor/Mortal-S42":
        raise ValueError("Download metadata does not name the fixed S42 release")
    files = {}
    for line in (source / "SHA256SUMS.txt").read_text().splitlines():
        expected, relative = line.split(maxsplit=1)
        relative = relative.strip().lstrip("*")
        path = (source / relative).resolve()
        if source not in path.parents or digest(path) != expected:
            raise ValueError(f"Published source checksum mismatch: {relative}")
        files[str(path.relative_to(bundle))] = expected
    weight = source / "weights/s42-inference.pth"
    if weight.stat().st_size != 43670035 or digest(weight) != WEIGHT_SHA:
        raise ValueError("Fixed S42 weight identity mismatch")
    files["source/SHA256SUMS.txt"] = digest(source / "SHA256SUMS.txt")
    files["source/.hfd/repo_metadata.json"] = digest(source / ".hfd/repo_metadata.json")
    completion = bundle / "source-completion.json"
    if args.complete_workspace and not completion.exists():
        completed = {}
        for relative, expected in WORKSPACE_COMPLETION.items():
            url = f"https://raw.githubusercontent.com/Equim-chan/Mortal/{MORTAL_COMMIT}/{relative}"
            data = urllib.request.urlopen(url, timeout=60).read(1024 * 1024)
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError("Fixed upstream workspace file checksum mismatch")
            path = source / "runtime" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.read_bytes() != data:
                raise ValueError("Refusing to overwrite a different workspace file")
            path.write_bytes(data)
            completed[str(path.relative_to(bundle))] = {"url": url, "sha256": expected, "bytes": len(data)}
        completion.write_text(json.dumps({"reason": "S42 workspace names exe-wrapper but omits it",
                                         "upstream_commit": MORTAL_COMMIT, "files": completed}, indent=2) + "\n")
    if completion.exists():
        extra = json.loads(completion.read_text())
        if extra["upstream_commit"] != MORTAL_COMMIT:
            raise ValueError("Unexpected source-completion upstream")
        for relative, item in extra["files"].items():
            path = (bundle / relative).resolve()
            if source not in path.parents or digest(path) != item["sha256"]:
                raise ValueError("Source completion checksum mismatch")
            files[relative] = item["sha256"]
        files["source-completion.json"] = digest(completion)
    target = storage.path("cache/phase2-mortal-s42/target")
    cargo_home = storage.path("cache/phase2-mortal-s42/cargo")
    cargo_home.mkdir(parents=True, exist_ok=True)
    target.mkdir(parents=True, exist_ok=True)
    cargo = args.cargo.resolve()
    environment = os.environ.copy()
    environment.update(storage.environment())
    environment.update(CARGO_HOME=str(cargo_home), CARGO_TARGET_DIR=str(target),
                       PYO3_PYTHON=str(python), PATH=str(cargo.parent) + os.pathsep + os.environ["PATH"])
    # Only the Python extension is needed. Package binaries cannot link with
    # extension-module's unresolved Python symbols on macOS.
    command = [str(cargo), "build", "--release", "--locked", "--lib", "-p", "libriichi"]
    started = time.monotonic()
    log_path = bundle / ("build-" + time.strftime("%Y%m%dT%H%M%S") + ".log")
    with log_path.open("x") as log:
        subprocess.run(command, cwd=source / "runtime", env=environment,
                       stdout=log, stderr=subprocess.STDOUT, check=True)
    details = json.loads(subprocess.check_output([
        str(python), "-I", "-B", "-c",
        "import json,sys,sysconfig,torch,numpy; print(json.dumps({'python':sys.version,"
        "'suffix':sysconfig.get_config_var('EXT_SUFFIX'),'torch':torch.__version__,'numpy':numpy.__version__}))"
    ], env=environment, text=True))
    library = "libriichi.dylib" if sys.platform == "darwin" else "libriichi.so"
    extension = source / "runtime/mortal" / ("libriichi" + details["suffix"])
    shutil.copy2(target / "release" / library, extension)
    files[str(extension.relative_to(bundle))] = digest(extension)
    build = {"command": command, "elapsed_seconds": time.monotonic() - started,
             "cargo": subprocess.check_output([str(cargo), "--version"], env=environment, text=True).strip(),
             "platform": platform.platform(), "packages": details,
             "cargo_target": str(target), "cargo_home": str(cargo_home),
             "log_path": str(log_path), "log_sha256": digest(log_path)}
    (bundle / "build-manifest.json").write_text(json.dumps(build, indent=2) + "\n")
    dependency_files = {}
    for path in sorted((bundle / "venv").rglob("*")):
        if path.is_file() and not path.is_symlink() and path.suffix != ".pyc":
            dependency_files[str(path.relative_to(bundle))] = digest(path)
    dependency_manifest = bundle / "environment-files.json"
    dependency_manifest.write_text(json.dumps({"schema_version": 1, "files": dependency_files},
                                               indent=2, sort_keys=True) + "\n")
    files["environment-files.json"] = digest(dependency_manifest)
    manifest = {"schema_version": 1, "kind": "mortal-runtime-v1", "checkout": "source",
                "upstream": {"repo": "haor/Mortal-S42", "revision": REVISION,
                             "mortal_commit": "0cff2b52982be5b1163aa9a62fb01f03ce91e0d2",
                             "weight_license": "Apache-2.0", "runtime_license": "AGPL-3.0-or-later"},
                "python": {"path": str(python), "sha256": digest(python.resolve())},
                "packages": details, "environment_manifest": "environment-files.json", "files": files}
    path = bundle / "runtime-manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    descriptor = {"factory": "agari_kit.opponents.mortal:MortalAgent",
                  "kwargs": {"threads": 1, "timeout_seconds": 10, "startup_timeout_seconds": 60},
                  "artifacts": {"manifest_path": {"path": "runtime-manifest.json", "sha256": digest(path)}}}
    (bundle / "agent.json").write_text(json.dumps(descriptor, indent=2) + "\n")
    print(json.dumps({"status": "built", "descriptor": str(bundle / "agent.json"), "build": build}))


if __name__ == "__main__":
    main()
