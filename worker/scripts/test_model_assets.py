#!/usr/bin/env python3
"""Exercise optional model preparation with a controlled S3 transport."""

import errno
import hashlib
from pathlib import Path
import sys
import tempfile
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

    def get_object(self, **request):
        byte_range = request.get("Range")
        self.ranges.append(byte_range)
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
