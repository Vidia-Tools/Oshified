#!/usr/bin/env python3
"""Build a content-addressed B2 manifest from a verified worker model directory."""

import argparse
import hashlib
import json
from pathlib import Path


MODEL_SUFFIXES = {".safetensors", ".ckpt", ".gguf", ".pt", ".pth", ".onnx", ".bin", ".pkl", ".model"}


def build_manifest(root, prefix, selectors):
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        extra = selectors.get(relative, {})
        if path.suffix.lower() not in MODEL_SUFFIXES and relative not in selectors:
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError(f"Copy symlinked model into the release explicitly: {relative}")
        before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
                digest.update(block)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError(f"Model changed during hashing: {relative}")
        if after.st_size == 0:
            raise ValueError(f"Model is empty: {relative}")
        sha = digest.hexdigest()
        files.append({"path": relative, "key": f"{prefix}/{sha}", "sha256": sha,
                      "size": after.st_size, **extra})
    missing = set(selectors) - {entry["path"] for entry in files}
    if missing or not files:
        raise ValueError(f"No usable model release, missing selector paths: {sorted(missing)}")
    return {"version": 1, "files": files}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("models", type=Path)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--selectors", type=Path, help="Path-keyed JSON with references/node_classes")
    args = parser.parse_args()
    root = args.models.resolve(strict=True)
    prefix = args.prefix.strip("/")
    if not root.is_dir() or not prefix or ".." in prefix.split("/"):
        parser.error("Use an existing model directory and a valid bucket prefix")
    selectors = json.loads(args.selectors.read_text()) if args.selectors else {}
    for path, spec in selectors.items():
        if set(spec) - {"references", "node_classes"}:
            parser.error(f"Unsupported selector fields for {path}")
        if any(not isinstance(values, list) or any(not isinstance(x, str) or not x for x in values)
               for values in spec.values()):
            parser.error(f"Selectors must be lists of nonempty strings for {path}")
    print(json.dumps(build_manifest(root, prefix, selectors), indent=2))


if __name__ == "__main__":
    main()
