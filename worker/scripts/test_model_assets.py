#!/usr/bin/env python3
"""Exercise optional model preparation with a controlled S3 transport."""

import errno
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from model_assets import ModelAssets, ModelPreparationError, _connected_to_outputs, prepare_models


class Body:
    def __init__(self, payload, interrupt):
        self.payload = payload
        self.interrupt = interrupt

    def iter_chunks(self, _size):
        if self.interrupt:
            yield self.payload[:2]
            raise OSError("interrupted")
        yield self.payload

    def close(self):
        pass


class Storage:
    def __init__(self, payload, interrupt=False):
        self.payload = payload
        self.interrupt = interrupt
        self.ranges = []
        self.keys = []

    def get_object(self, **request):
        byte_range = request.get("Range")
        self.ranges.append(byte_range)
        self.keys.append(request["Key"])
        start = int(byte_range[6:-1]) if byte_range else 0
        payload = self.payload[start:]
        response = {
            "Body": Body(payload, self.interrupt and not byte_range),
            "ContentLength": len(payload),
        }
        if byte_range:
            response["ContentRange"] = f"bytes {start}-{len(self.payload) - 1}/{len(self.payload)}"
        return response


class ModelAssetsTests(unittest.TestCase):
    payload = b"verified checkpoint"
    relative_path = "checkpoints/example.safetensors"
    graph = {
        "1": {"class_type": "Loader", "inputs": {"ckpt_name": "example.safetensors"}},
        "2": {"class_type": "Save", "inputs": {"model": ["1", 0]}},
        "3": {"class_type": "Loader", "inputs": {"ckpt_name": "unused.safetensors"}},
    }

    def store(self, root, client, *aliases):
        return ModelAssets({
            "version": 1,
            "files": [{
                "path": path,
                "key": "example",
                "size": len(self.payload),
                "sha256": hashlib.sha256(self.payload).hexdigest(),
            } for path in (self.relative_path, *aliases)],
        }, root, client, "fixture")

    def configured_store(self, root, storage, entries, prefix=None):
        manifest = Path(root) / "manifest.json"
        manifest.write_text(json.dumps({"version": 1, "files": entries}))
        env = {
            "MODEL_STORE_MANIFEST": str(manifest),
            "MODEL_STORE_ROOT": str(Path(root) / "models"),
            "MODEL_STORE_ENDPOINT": "https://s3.example.test",
            "MODEL_STORE_REGION": "test",
            "MODEL_STORE_BUCKET": "fixture",
            "MODEL_STORE_KEY_ID": "fixture-id",
            "MODEL_STORE_APP_KEY": "fixture-secret",
        }
        if prefix is not None:
            env["MODEL_STORE_OBJECT_PREFIX"] = prefix
        boto3 = ModuleType("boto3")
        boto3.client = lambda *_args, **_kwargs: storage
        config = ModuleType("botocore.config")
        config.Config = dict
        with patch.dict("os.environ", env, clear=True), patch.dict(sys.modules, {
            "boto3": boto3, "botocore.config": config,
        }):
            return ModelAssets.from_env()

    def test_from_env_rewrites_only_content_addressed_object_keys(self):
        sha = hashlib.sha256(self.payload).hexdigest()
        entry = {
            "path": self.relative_path,
            "key": f"models/{sha}",
            "size": len(self.payload),
            "sha256": sha,
            "references": ["old-name.safetensors"],
            "node_classes": ["FixedAssetLoader"],
        }
        keys = (f"custom/{'a' * 64}", f"/models/{sha}", f"models//{sha}",
                f"models/./{sha}", f"models/../{sha}", f"models\\other/{sha}",
                f"models\n/{sha}", f"{sha}/other", "models/legacy.safetensors", sha)
        others = [{**entry, "key": key, "path": f"aliases/{index}.safetensors",
                   "references": [], "node_classes": []} for index, key in enumerate(keys)]
        storage = Storage(self.payload)
        with tempfile.TemporaryDirectory() as root:
            store = self.configured_store(root, storage, [entry, *others], "oshified/sha256")
            expected = {**entry, "key": f"oshified/sha256/{sha}"}
            rewritten_bare_key = {**others[-1], "key": expected["key"]}
            self.assertEqual(list(store.entries.values()), [expected, *others[:-1], rewritten_bare_key])
            for graph in (
                {"1": {"class_type": "Loader", "inputs": {"ckpt_name": "old-name.safetensors"}}},
                {"1": {"class_type": "FixedAssetLoader", "inputs": {}}},
            ):
                self.assertEqual(store.required(graph), [expected])
            store.prepare(graph, lambda *_: None)
            self.assertEqual(storage.keys, [expected["key"]])
            self.assertEqual((Path(root) / "models" / self.relative_path).read_bytes(), self.payload)

    def test_from_env_unset_or_empty_object_prefix_uses_manifest_key(self):
        sha = hashlib.sha256(self.payload).hexdigest()
        entry = {"path": self.relative_path, "key": f"models/{sha}", "size": len(self.payload), "sha256": sha}
        for prefix in (None, ""):
            with self.subTest(prefix=prefix), tempfile.TemporaryDirectory() as root:
                storage = Storage(self.payload)
                store = self.configured_store(root, storage, [entry], prefix)
                store.prepare(_connected_to_outputs(self.graph, {"Save"}), lambda *_: None)
                self.assertEqual(storage.keys, [entry["key"]])

    def test_from_env_rejects_noncanonical_object_prefix(self):
        entry = {"path": self.relative_path, "key": "legacy", "size": 1, "sha256": "a" * 64}
        with tempfile.TemporaryDirectory() as root:
            for prefix in ("/absolute", "trailing/", "nested//prefix", "nested/../prefix",
                           "nested/./prefix", ".", "..", "nested\\prefix", "prefix\n"):
                with self.subTest(prefix=prefix), self.assertRaisesRegex(ValueError, "MODEL_STORE_OBJECT_PREFIX"):
                    self.configured_store(root, None, [entry], prefix)

    def test_connected_cold_resume_and_warm_reuse(self):
        graph = _connected_to_outputs(self.graph, {"Save"})
        self.assertEqual(set(graph), {"1", "2"})
        storage = Storage(self.payload, interrupt=True)
        with tempfile.TemporaryDirectory() as root:
            store = self.store(root, storage)
            events = []
            store.prepare(graph, lambda _, event: events.append(event))
            self.assertEqual(storage.ranges, [None, "bytes=2-"])
            self.assertEqual((Path(root) / self.relative_path).read_bytes(), self.payload)
            self.assertEqual(events[0]["bytesReceived"], 0)
            self.assertEqual(events[-2]["bytesReceived"], len(self.payload))
            self.assertEqual(events[-1], {"phase": "loading"})
            store.prepare(graph, lambda _, event: events.append(event))
            self.assertEqual(storage.ranges, [None, "bytes=2-"])

    def test_bad_hash_is_not_cached(self):
        graph = _connected_to_outputs(self.graph, {"Save"})
        storage = Storage(b"x" * len(self.payload))
        with tempfile.TemporaryDirectory() as root:
            store = self.store(root, storage)
            with self.assertRaisesRegex(ModelPreparationError, "SHA-256"):
                store.prepare(graph, lambda *_: None)
            self.assertEqual(list((Path(root) / "checkpoints").iterdir()), [])

    def test_cold_graph_downloads_shared_model_bytes_once(self):
        alias = "checkpoints/other.safetensors"
        graph = {
            "1": {"class_type": "Loader", "inputs": {"ckpt_name": "example.safetensors"}},
            "2": {"class_type": "Loader", "inputs": {"ckpt_name": "other.safetensors"}},
        }
        storage = Storage(self.payload)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events = []
            store = self.store(root, storage, alias)
            store.prepare(graph, lambda _, event: events.append(event))
            self.assertEqual(storage.ranges, [None])
            self.assertEqual((root / alias).read_bytes(), self.payload)
            self.assertEqual([e["totalBytes"] for e in events if e["phase"] == "downloading"],
                             [len(self.payload), len(self.payload)])

    def test_alias_reuses_verified_model_from_another_stage(self):
        alias = "another-folder/other.safetensors"
        primary = _connected_to_outputs(self.graph, {"Save"})
        alias_graph = {"1": {"class_type": "Loader", "inputs": {"ckpt_name": "other.safetensors"}}}
        storage = Storage(self.payload)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.store(root, storage, alias)
            store.prepare(primary, lambda *_: None)

            events = []
            store = self.store(root, storage, alias)
            store.prepare(alias_graph, lambda _, event: events.append(event))
            self.assertEqual(events, [{"phase": "loading"}])
            self.assertEqual(storage.ranges, [None])
            self.assertTrue((root / alias).samefile(root / self.relative_path))

            (root / self.relative_path).unlink()
            (root / self.relative_path).write_bytes(b"different checkpoint")
            store.prepare(alias_graph, lambda *_: None)
            self.assertEqual(storage.ranges, [None])
            self.assertEqual((root / alias).read_bytes(), self.payload)

    def test_cache_without_hardlinks_copies_verified_alias_locally(self):
        alias = "checkpoints/other.safetensors"
        graph = {
            "1": {"class_type": "Loader", "inputs": {"ckpt_name": "example.safetensors"}},
            "2": {"class_type": "Loader", "inputs": {"ckpt_name": "other.safetensors"}},
        }
        storage = Storage(self.payload)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.store(root, storage, alias)
            with patch("model_assets.os.link", side_effect=OSError(errno.EOPNOTSUPP, "unsupported")):
                store.prepare(graph, lambda *_: None)
            self.assertEqual(storage.ranges, [None])
            self.assertEqual((root / alias).read_bytes(), self.payload)
            self.assertFalse((root / alias).samefile(root / self.relative_path))

    def test_local_models_do_not_require_storage(self):
        with patch.dict("os.environ", {}, clear=True), patch.object(ModelAssets, "from_env") as from_env:
            prepare_models(self.graph, None, None)
        from_env.assert_not_called()


if __name__ == "__main__":
    unittest.main()
