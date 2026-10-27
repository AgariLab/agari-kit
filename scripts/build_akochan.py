"""Build the pinned, unmodified Akochan sources using Apple clang and local deps."""

import argparse
import concurrent.futures
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

COMMIT = "53188a0b926fbab38177f88c3cd87d554cf412af"


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args()
    root = args.run_root.resolve()
    source = root / ("akochan-" + COMMIT)
    deps = root / "build-deps"
    build = root / "build"
    build.mkdir(exist_ok=True)
    (build / "logs").mkdir(exist_ok=True)
    original = json.loads((root / "source-manifest.json").read_text())
    for relative, expected in original["files"].items():
        if sha256(source / relative) != expected:
            raise RuntimeError("Original source changed: " + relative)
    for relative in ("include/boost/asio.hpp", "include/omp.h", "lib/libomp.dylib"):
        if not (deps / relative).is_file():
            raise RuntimeError("Missing local dependency: " + relative)
    paths = sorted(list(source.glob("*.cpp")) + list((source / "share").glob("*.cpp"))
                   + list((source / "ai_src").glob("*.cpp")) + list((source / "ai_src/learn").glob("*.cpp")))
    flags = ["-std=c++11", "-O3", "-mcpu=apple-m1", "-fPIC", "-pthread",
             "-DNPROCS=" + str(args.threads), "-DBOOST_ERROR_CODE_HEADER_ONLY", "-DBOOST_BIND_GLOBAL_PLACEHOLDERS",
             "-Xpreprocessor", "-fopenmp", "-I" + str(deps / "include"), "-I" + str(source)]
    commands = [{"source": str(path), "object": str(build / (path.relative_to(source).as_posix().replace("/", "__") + ".o")),
                 "argv": ["/usr/bin/clang++", *flags, "-c", str(path), "-o",
                          str(build / (path.relative_to(source).as_posix().replace("/", "__") + ".o"))]}
                for path in paths]
    (root / "compile-commands.json").write_text(json.dumps(commands, ensure_ascii=False, indent=2) + "\n")
    env = os.environ.copy()
    env.update({"TMPDIR": str(root / "tmp"), "CLANG_MODULE_CACHE_PATH": str(root / "cache/clang")})
    started = time.monotonic()

    def compile_one(command):
        log = build / "logs" / (Path(command["object"]).name + ".log")
        with log.open("w") as output:
            result = subprocess.run(command["argv"], cwd=source, env=env, stdout=output, stderr=subprocess.STDOUT)
        print(json.dumps({"source": Path(command["source"]).name, "exit_code": result.returncode}), flush=True)
        return result.returncode, log

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        outcomes = list(pool.map(compile_one, commands))
    failed = [str(log) for status, log in outcomes if status]
    if failed:
        raise RuntimeError("Compilation failed; logs: " + ", ".join(failed))
    binary = source / "system.exe"
    link = ["/usr/bin/clang++", *[command["object"] for command in commands], "-pthread",
            "-L" + str(deps / "lib"), "-Wl,-rpath," + str(deps / "lib"), "-lomp", "-o", str(binary)]
    with (build / "logs/link.log").open("w") as output:
        subprocess.run(link, cwd=source, env=env, stdout=output, stderr=subprocess.STDOUT, check=True)
    # A single executable avoids changing the source or relying on a separate libai.so.
    report = {"schema_version": 1, "commit": COMMIT, "source_unmodified": True,
              "link_mode": "all original translation units in one executable", "threads_compiled": args.threads,
              "compiler": subprocess.check_output(["/usr/bin/clang++", "--version"], text=True),
              "flags": flags, "link_argv": link, "translation_units": len(paths),
              "elapsed_seconds": time.monotonic() - started,
              "binary": str(binary), "binary_sha256": sha256(binary),
              "openmp_library_sha256": sha256(deps / "lib/libomp.dylib"),
              "dependency_records": json.loads((root / "dependencies-manifest.json").read_text())["packages"]}
    (root / "build-manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: report[key] for key in ("binary", "binary_sha256", "translation_units", "elapsed_seconds")}))


if __name__ == "__main__":
    main()
