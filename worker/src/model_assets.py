"""Download only the verified model assets required by an API workflow graph."""

import errno
import fcntl
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import time
from urllib.parse import urlsplit


_EXTENSIONS = {".safetensors", ".ckpt", ".gguf", ".pt", ".pth", ".onnx", ".bin", ".pkl", ".model"}
_BLOCK = 8 * 1024 * 1024


class ModelPreparationError(RuntimeError):
    pass


def _canonical_object_path(value):
    path = PurePosixPath(value)
    return (bool(path.parts) and not path.is_absolute() and ".." not in path.parts
            and path.as_posix() == value and "\\" not in value
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


class ModelAssets:
    def __init__(self, manifest, root, client, bucket, *, object_prefix=""):
        if object_prefix and not _canonical_object_path(object_prefix):
            raise ValueError("MODEL_STORE_OBJECT_PREFIX must be a canonical relative path")
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.client = client
        self.bucket = bucket
        self.entries = {}
        self.by_ref = {}
        self.by_node = {}
        self.by_content = {}
        if manifest.get("version") != 1 or not manifest.get("files"):
            raise ValueError("Invalid model manifest")
        for spec in manifest["files"]:
            path = PurePosixPath(spec["path"])
            if path.is_absolute() or ".." in path.parts or len(path.parts) < 2:
                raise ValueError("Model paths must be relative to the models directory")
            if path.as_posix() != spec["path"] or "\\" in spec["path"]:
                raise ValueError("Model paths must be canonical")
            size, sha, key = spec["size"], spec["sha256"], spec["key"]
            if type(size) is not int or size < 1 or not re.fullmatch(r"[a-f0-9]{64}", sha):
                raise ValueError("Invalid model size or SHA-256")
            if not isinstance(key, str) or not key:
                raise ValueError("Invalid object key")
            if object_prefix and key.rsplit("/", 1)[-1] == sha and _canonical_object_path(key):
                spec = {**spec, "key": f"{object_prefix}/{sha}"}
            name = path.as_posix()
            if name in self.entries:
                raise ValueError(f"Duplicate model path: {name}")
            self.entries[name] = spec
            self.by_content.setdefault((sha, size), []).append(spec)
            for ref in {name, path.name, *spec.get("references", [])}:
                self.by_ref.setdefault(ref.replace("\\", "/"), set()).add(name)
            for class_type in spec.get("node_classes", []):
                self.by_node.setdefault(class_type, set()).add(name)

    @classmethod
    def from_env(cls):
        manifest_path = os.environ["MODEL_STORE_MANIFEST"]
        endpoint = urlsplit(os.environ["MODEL_STORE_ENDPOINT"])
        if endpoint.scheme != "https" or not endpoint.hostname or endpoint.username:
            raise ValueError("Model store endpoint must be HTTPS")
        import boto3
        from botocore.config import Config
        client = boto3.client(
            "s3", endpoint_url=endpoint.geturl(), region_name=os.environ["MODEL_STORE_REGION"],
            aws_access_key_id=os.environ["MODEL_STORE_KEY_ID"],
            aws_secret_access_key=os.environ["MODEL_STORE_APP_KEY"],
            config=Config(connect_timeout=10, read_timeout=45,
                          retries={"mode": "standard", "max_attempts": 2}),
        )
        return cls(json.loads(Path(manifest_path).read_text()), os.environ["MODEL_STORE_ROOT"],
                   client, os.environ["MODEL_STORE_BUCKET"],
                   object_prefix=os.environ.get("MODEL_STORE_OBJECT_PREFIX", ""))

    def required(self, graph):
        selected = set()
        for node in graph.values():
            if not isinstance(node, dict):
                continue
            class_type = node.get("class_type", "")
            selected.update(self.by_node.get(class_type, ()))
            for field, value in node.get("inputs", {}).items():
                if not isinstance(value, str):
                    continue
                ref = value.replace("\\", "/")
                matches = self.by_ref.get(ref)
                if matches is None:
                    matches = self.by_ref.get(PurePosixPath(ref).name)
                if matches:
                    if len(matches) != 1:
                        raise ModelPreparationError(f"Ambiguous model reference in {class_type}.{field}")
                    selected.update(matches)
                elif (("model" in field or field.rstrip("0123456789") in {
                    "ckpt_name", "unet_name", "lora_name", "vae_name", "clip_name", "control_net_name"
                }) and PurePosixPath(ref).suffix.lower() in _EXTENSIONS and "://" not in ref):
                    raise ModelPreparationError(f"Missing model manifest entry for {class_type}.{field}")
        return [self.entries[name] for name in sorted(selected)]

    def prepare(self, graph, send_progress):
        required = self.required(graph)
        if not required:
            return
        with (self.root / ".model-store.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            pending = {}
            for spec in required:
                if not self._verified(spec):
                    identity = spec["sha256"], spec["size"]
                    pending.setdefault(identity, []).append(spec)
            cached = {
                identity: next((spec for spec in self.by_content[identity] if self._verified(spec)), None)
                for identity in pending
            }
            total = sum(size for (_, size), source in cached.items() if source is None)
            if total > shutil.disk_usage(self.root).free:
                raise ModelPreparationError("Worker has insufficient disk space for required models")
            done = 0
            sent_at = 0.0

            def publish(value, always=False):
                nonlocal sent_at
                now = time.monotonic()
                if always or now - sent_at >= 1:
                    send_progress("model_preparation", {
                        "phase": "downloading", "bytesReceived": value, "totalBytes": total
                    })
                    sent_at = now

            if total:
                publish(0, always=True)
            for identity, targets in pending.items():
                source = cached[identity]
                if source is None:
                    source, *targets = targets
                    self._download(source, lambda count: publish(done + count))
                    done += source["size"]
                    publish(done, always=done == total)
                for spec in targets:
                    self._reuse_verified(source, spec)
            send_progress("model_preparation", {"phase": "loading"})

    def _paths(self, spec):
        path = self.root / spec["path"]
        if not path.resolve().is_relative_to(self.root):
            raise ModelPreparationError("Model path escapes the worker cache")
        return path, path.with_name("." + path.name + ".verified")

    def _verified(self, spec):
        path, receipt = self._paths(spec)
        try:
            stat = path.stat()
            return (
                path.is_file()
                and stat.st_size == spec["size"]
                and receipt.read_text() == f'{spec["sha256"]} {stat.st_mtime_ns}\n'
            )
        except FileNotFoundError:
            return False

    def _reuse_verified(self, source, spec):
        source_path, _ = self._paths(source)
        source_path = source_path.resolve(strict=True)
        path, receipt = self._paths(spec)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path != source_path:
            tmp = path.with_name("." + path.name + ".alias")
            try:
                tmp.unlink(missing_ok=True)
                try:
                    os.link(source_path, tmp)
                except OSError as exc:
                    if exc.errno not in {errno.EXDEV, errno.EPERM, errno.EOPNOTSUPP, errno.EMLINK}:
                        raise
                    if spec["size"] > shutil.disk_usage(path.parent).free:
                        raise ModelPreparationError("Worker has insufficient disk space for required models") from exc
                    shutil.copyfile(source_path, tmp)
                tmp.replace(path)
            finally:
                tmp.unlink(missing_ok=True)
        receipt.write_text(f'{spec["sha256"]} {path.stat().st_mtime_ns}\n')

    def _download(self, spec, progress):
        path, receipt = self._paths(spec)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name("." + path.name + ".partial")
        digest = hashlib.sha256()
        received = 0
        try:
            with tmp.open("wb") as file:
                for attempt in range(2):
                    body = None
                    try:
                        args = {"Bucket": self.bucket, "Key": spec["key"]}
                        if received:
                            args["Range"] = f"bytes={received}-"
                        response = self.client.get_object(**args)
                        body = response["Body"]
                        if (response.get("ContentLength") != spec["size"] - received or
                            (received and response.get("ContentRange") !=
                             f'bytes {received}-{spec["size"] - 1}/{spec["size"]}')):
                            raise ModelPreparationError("Unexpected model response from storage")
                        for chunk in body.iter_chunks(_BLOCK):
                            if received + len(chunk) > spec["size"]:
                                raise ModelPreparationError("Model exceeds its declared size")
                            file.write(chunk)
                            digest.update(chunk)
                            received += len(chunk)
                            progress(received)
                        if received == spec["size"]:
                            break
                    except ModelPreparationError:
                        raise
                    except Exception:
                        if attempt == 1:
                            raise ModelPreparationError("Model download interrupted") from None
                    finally:
                        if body is not None:
                            body.close()
                if received != spec["size"] or digest.hexdigest() != spec["sha256"]:
                    raise ModelPreparationError("Model failed size or SHA-256 verification")
            tmp.replace(path)
            receipt.write_text(f'{spec["sha256"]} {path.stat().st_mtime_ns}\n')
        finally:
            tmp.unlink(missing_ok=True)


_store = None


@lru_cache(maxsize=4)
def _comfy_output_types(host):
    import requests
    try:
        response = requests.get(f"http://{host}/object_info", timeout=20)
        response.raise_for_status()
        info = response.json()
        return {name for name, node in info.items() if node.get("output_node")}
    except (requests.RequestException, AttributeError, ValueError) as exc:
        raise ModelPreparationError("Could not resolve installed ComfyUI nodes") from exc


def _connected_to_outputs(graph, output_types):
    pending = [node_id for node_id, node in graph.items()
               if isinstance(node, dict) and node.get("class_type") in output_types]
    if not pending:
        raise ModelPreparationError("Workflow has no installed output node")
    selected = {}
    while pending:
        node_id = pending.pop()
        if node_id in selected:
            continue
        node = graph[node_id]
        selected[node_id] = node
        pending.extend(value[0] for value in node.get("inputs", {}).values()
                       if isinstance(value, list) and len(value) == 2
                       and isinstance(value[0], str) and type(value[1]) is int
                       and value[0] in graph and isinstance(graph[value[0]], dict))
    return selected


def prepare_models(graph, relay, comfy_host):
    if not os.environ.get("MODEL_STORE_MANIFEST"):
        return
    global _store
    if _store is None:
        try:
            _store = ModelAssets.from_env()
        except (KeyError, OSError, ValueError) as exc:
            raise ModelPreparationError("Worker model store is misconfigured") from exc
    graph = _connected_to_outputs(graph, _comfy_output_types(comfy_host))
    _store.prepare(graph, relay.send_progress)
