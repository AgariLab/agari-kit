"""Load a user-selected Agent factory without depending on its algorithm package."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import re
from pathlib import Path


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _resolve_descriptor(descriptor_path: str) -> tuple[dict, dict]:
    """Share the exact descriptor and artifact checks used by both public APIs."""
    path = Path(descriptor_path).expanduser().resolve()
    content = path.read_bytes()
    descriptor = json.loads(content)
    if not isinstance(descriptor, dict):
        raise ValueError("Agent descriptor must be an object")
    factory = descriptor.get("factory", "")
    if not isinstance(factory, str) or not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", factory):
        raise ValueError("Agent descriptor needs a module:factory name")
    kwargs = descriptor.get("kwargs", {})
    artifacts = descriptor.get("artifacts", {})
    if not isinstance(kwargs, dict) or not isinstance(artifacts, dict) or set(kwargs) & set(artifacts):
        raise ValueError("Invalid or conflicting Agent arguments")
    kwargs = dict(kwargs)
    resolved_artifacts = {}
    for name, artifact in artifacts.items():
        if not isinstance(artifact, dict) or set(artifact) != {"path", "sha256"}:
            raise ValueError("Each model artifact needs path and sha256")
        if not isinstance(artifact["path"], str) or not artifact["path"]:
            raise ValueError("Model artifact path must be a nonempty relative path")
        if not isinstance(artifact["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"]):
            raise ValueError("Model artifact needs a lowercase SHA-256")
        relative = Path(artifact["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Model artifacts must be packaged beside the descriptor")
        target = (path.parent / relative).resolve()
        if path.parent not in target.parents:
            raise ValueError("Model artifact escapes descriptor directory")
        actual_sha256 = _file_sha256(target)
        if actual_sha256 != artifact["sha256"]:
            raise ValueError("Model artifact hash mismatch")
        kwargs[name] = str(target)
        resolved_artifacts[name] = {"path": str(target), "declared_path": artifact["path"],
                                    "declared_sha256": artifact["sha256"], "sha256": actual_sha256}
    return kwargs, {"descriptor_path": str(path), "descriptor_sha256": hashlib.sha256(content).hexdigest(),
                    "descriptor": descriptor, "factory": factory, "artifacts": resolved_artifacts}


def _package_source(module_name: str) -> dict:
    """Fingerprint only the factory's Python package, excluding its dependencies."""
    package_name = module_name.split(".", 1)[0]
    package = importlib.util.find_spec(package_name)
    module = importlib.util.find_spec(module_name)
    if package is None or module is None or not module.origin or Path(module.origin).suffix != ".py":
        raise ValueError("Agent factory must have inspectable local Python source")
    digest = hashlib.sha256()
    count = 0
    if package.submodule_search_locations is None:
        roots = [Path(module.origin).resolve().parent]
        sources = [(Path(module.origin).name, Path(module.origin).resolve())]
    else:
        roots = [Path(root).resolve() for root in package.submodule_search_locations]
        sources = [(f"{index}/{source.relative_to(root).as_posix()}", source)
                   for index, root in enumerate(roots) for source in sorted(root.rglob("*.py"))]
    for relative, source in sources:
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(source.read_bytes())
        digest.update(b"\0")
        count += 1
    if not count:
        raise ValueError("Agent factory package contains no Python source")
    return {"package": package_name, "module": module_name, "module_path": str(Path(module.origin).resolve()),
            "roots": [str(root) for root in roots], "source_sha256": digest.hexdigest(), "files": count}


def agent_provenance(name: str) -> dict:
    """Identify agent configuration, actual weights and local implementation."""
    from .agent import AGENT_NAMES

    if name in AGENT_NAMES:
        return {"kind": "builtin", "name": name}
    if not name.startswith("@"):
        raise ValueError(f"Unknown agent: {name}")
    _, provenance = _resolve_descriptor(name[1:])
    module_name = provenance["factory"].split(":", 1)[0]
    return {"kind": "external", "name": name, **provenance, "source": _package_source(module_name)}


def load_agent(descriptor_path: str):
    kwargs, provenance = _resolve_descriptor(descriptor_path)
    factory = provenance["factory"]
    module_name, factory_name = factory.split(":")
    instance = getattr(importlib.import_module(module_name), factory_name)(**kwargs)
    if not callable(getattr(instance, "reset", None)) or not callable(getattr(instance, "act", None)):
        raise ValueError("Agent factory must provide reset and act")
    return instance
