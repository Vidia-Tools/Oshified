#!/usr/bin/env python3
"""Assemble the Oshified model-release build context from reviewed local inputs."""

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile


DEPENDENCIES = (
    "comfyui-videohelpersuite",
    "comfyui_controlnet_aux",
    "comfyui_essentials",
)
NODE_SLOT = "vidia-open-studio-node"
SOURCE_SUFFIXES = {".py", ".json", ".yaml", ".yml", ".txt", ".md", ".toml", ".js", ".ttf"}
NOTICE_NAMES = {"LICENSE", "NOTICE", "COPYING"}
FONT_SHA256 = {
    "comfyui_controlnet_aux/NotoSans-Regular.ttf": "6b04c8dd65af6b73eb4279472ed1580b29102d6496a377340e80a40cdb3b22c9",
    "comfyui_essentials/fonts/ShareTechMono-Regular.ttf": "911a85f56c44e8a712458513e6972a3e296ee9a84e70754462d03de3f3609c2b",
}


def is_source(path):
    if any(part.startswith(".") or part == "__pycache__" for part in path.parts):
        return False
    return path.suffix in SOURCE_SUFFIXES or path.name in NOTICE_NAMES


def copy_dependency(source, target):
    if source.is_symlink() or not (source / "__init__.py").is_file():
        raise ValueError(f"Missing dependency package: {source}")
    for entry in sorted(source.rglob("*")):
        relative = entry.relative_to(source)
        if entry.is_symlink():
            raise ValueError(f"Unexpected symbolic link: {entry}")
        if entry.is_file() and is_source(relative):
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(entry, destination)


def export_node(repo, revision, target):
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("--node-revision must be the full Git commit SHA")
    kind = subprocess.check_output(["git", "-C", repo, "cat-file", "-t", revision], text=True).strip()
    if kind != "commit":
        raise ValueError("--node-revision must identify a commit")
    archive = subprocess.check_output(["git", "-C", repo, "archive", revision])
    with tarfile.open(fileobj=io.BytesIO(archive)) as node_source:
        for member in node_source:
            relative = Path(member.name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"Unsafe node path: {member.name}")
            if not member.isdir() and not member.isfile():
                raise ValueError(f"Unexpected node archive entry: {member.name}")
            if member.isfile() and is_source(relative):
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                with node_source.extractfile(member) as source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output)
    if not (target / "oshified.py").is_file() or not (target / "__init__.py").is_file():
        raise ValueError("The Git commit does not contain the Oshified node pack")
    requirements = target / "requirements.txt"
    if not re.search(r"^yt-dlp\[default\]==[\d.]+\s*$", requirements.read_text(), re.MULTILINE):
        raise ValueError("Oshified node requirements must pin yt-dlp with its default extra")


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def add_font_notices(nodes):
    notices = Path(__file__).resolve().parents[1] / "licenses"
    for relative, expected in FONT_SHA256.items():
        font = nodes / relative
        notice = font.with_suffix(".OFL.txt")
        if not font.is_file() or notice.is_file():
            continue
        if sha256(font) != expected:
            raise ValueError(f"Include the matching license notice next to {relative}")
        shutil.copyfile(notices / notice.name, notice)


def package_digest(directory):
    rows = [
        (file.relative_to(directory).as_posix(), sha256(file))
        for file in sorted(directory.rglob("*"))
        if file.is_file()
    ]
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def package(dependencies, node_repo, node_revision, output):
    manifest = dependencies / "manifest.json"
    if manifest.is_symlink() or not manifest.is_file():
        raise ValueError("Expected a review-approved manifest.json in the dependency release")
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"Choose a new output directory: {output}")

    with tempfile.TemporaryDirectory(prefix=".oshified-release-", dir=output.parent) as temporary:
        context = Path(temporary) / "context"
        nodes = context / "nodes"
        nodes.mkdir(parents=True)
        shutil.copyfile(manifest, context / "manifest.json")
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from model_assets import ModelAssets
        ModelAssets(json.loads((context / "manifest.json").read_text()),
                    Path(temporary) / "manifest-validation", client=None, bucket=None)
        for directory in DEPENDENCIES:
            copy_dependency(dependencies / "nodes" / directory, nodes / directory)
        export_node(node_repo, node_revision, nodes / NODE_SLOT)
        add_font_notices(nodes)
        receipt = {
            "manifest_sha256": sha256(context / "manifest.json"),
            "node_revision": node_revision,
            "node_package_sha256": {
                directory: package_digest(nodes / directory)
                for directory in (*DEPENDENCIES, NODE_SLOT)
            },
        }
        (context / "release-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        context.rename(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dependencies", type=Path, required=True)
    parser.add_argument("--node-repo", type=Path, required=True)
    parser.add_argument("--node-revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        package(args.dependencies, args.node_repo, args.node_revision, args.output)
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        parser.exit(1, f"{exc}\n")
    print(args.output.absolute())


if __name__ == "__main__":
    main()
