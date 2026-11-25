#!/usr/bin/env python3
"""Exercise the local Oshified HTTP contract without ComfyUI or a GPU."""

import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import app_server


ORIGIN = "http://localhost:3000"
JOBS = "/api/oshified/jobs"
BOUNDARY = "oshified-local-smoke"


def encode_form(fields, files):
    chunks = []
    for key, value in fields.items():
        chunks.append(f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    for key, (name, content_type, data) in files.items():
        chunks.extend([f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="{key}"; filename="{name}"\r\n'
                       f'Content-Type: {content_type}\r\n\r\n'.encode(), data, b"\r\n"])
    chunks.append(f"--{BOUNDARY}--\r\n".encode())
    return b"".join(chunks)


class LocalJobsTests(unittest.TestCase):
    def test_upload_progress_scoped_media_and_ranges(self):
        with TemporaryDirectory() as workspace:
            root = Path(workspace)
            paths = {variable: str(root / (slot + ".png"))
                     for slot, variable in app_server.LOCAL_ASSETS.items()}
            for value in paths.values():
                Path(value).write_bytes(b"operator fixture")

            allow_clip = threading.Event()
            fixture_bound = threading.Event()
            still, clip = root / "still.jpg", root / "clip.mp4"
            still.write_bytes(b"portrait")
            clip.write_bytes(b"abcdefgh")

            def fake_run(validated, on_event=None, on_artifact=None):
                params = validated["params"]
                self.assertEqual(params["method"], "vtuber-to-real")
                staged = app_server.runner.download_files(params["files"], root / "downloads")
                self.assertEqual(Path(staged["in_image"]).read_bytes(), b"original")
                self.assertEqual(Path(staged["in_audio"]).read_bytes(), b"sound")
                self.assertEqual(Path(staged["in_pose"]).read_bytes(), b"operator fixture")
                on_event("stage_start", {"stageName": "clip"})
                on_artifact("still", str(still))
                fixture_bound.set()
                self.assertTrue(allow_clip.wait(10))
                on_artifact("clip", str(clip))
                return {"artifacts": {"still": str(still), "clip": str(clip)}}

            with patch.dict(os.environ, paths), patch.object(app_server, "run_generation", fake_run), \
                    patch.object(app_server.runner, "COMFY_OUTPUT_DIR", str(root)):
                with app_server.ThreadingHTTPServer(("127.0.0.1", 0), app_server.AppHandler) as server:
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    try:
                        base = f"http://127.0.0.1:{server.server_port}"
                        body = encode_form({"mode": "vtuber-to-real", "variant": "female"}, {
                            "image": ("original.png", "image/png", b"original"),
                            "audio": ("input.wav", "audio/wav", b"sound"),
                        })

                        def submit(origin, extra=None):
                            return urlopen(Request(base + JOBS, data=body, headers={
                                "Content-Type": f"multipart/form-data; boundary={BOUNDARY}",
                                "Origin": origin,
                                **(extra or {}),
                            }), timeout=4)

                        with self.assertRaises(HTTPError) as blocked:
                            submit("https://untrusted.test")
                        with blocked.exception as response:
                            self.assertEqual(response.code, 403)
                        with self.assertRaises(HTTPError) as rebound:
                            submit(ORIGIN, {"Host": f"untrusted.test:{server.server_port}"})
                        with rebound.exception as response:
                            self.assertEqual(response.code, 403)
                        with urlopen(Request(base + JOBS, method="OPTIONS", headers={"Origin": ORIGIN}),
                                     timeout=4) as response:
                            self.assertEqual(response.status, 204)
                            self.assertIn("Authorization", response.headers["Access-Control-Allow-Headers"])
                        with submit(ORIGIN) as response:
                            self.assertEqual(response.status, 202)
                            self.assertEqual(response.headers["Access-Control-Allow-Origin"], ORIGIN)
                            job = json.load(response)

                        status_url = f"{base}{JOBS}/{job['id']}"
                        headers = {"Authorization": f"Bearer {job['access']}", "Origin": ORIGIN}
                        self.assertTrue(fixture_bound.wait(5), "worker never reached its still artifact")
                        with self.assertRaises(HTTPError) as unauthorized:
                            urlopen(status_url, timeout=4)
                        with unauthorized.exception as response:
                            self.assertEqual(response.code, 404)
                        with urlopen(Request(status_url, headers=headers), timeout=4) as response:
                            snapshot = json.load(response)
                        self.assertEqual(snapshot["status"], "running")
                        self.assertEqual(snapshot["progress"]["stageName"], "clip")
                        with urlopen(snapshot["artifacts"]["still"] + "&download=1", timeout=4) as response:
                            self.assertEqual(response.headers["Content-Disposition"],
                                             'attachment; filename="oshified-portrait.jpg"')
                            self.assertEqual(response.read(), b"portrait")

                        allow_clip.set()
                        deadline = time.monotonic() + 4
                        while time.monotonic() < deadline:
                            with urlopen(Request(status_url, headers=headers), timeout=4) as response:
                                snapshot = json.load(response)
                            if snapshot["status"] == "complete":
                                break
                            time.sleep(0.01)
                        self.assertEqual(snapshot["status"], "complete")
                        with urlopen(Request(snapshot["artifacts"]["clip"], headers={"Range": "bytes=2-4"}),
                                     timeout=4) as response:
                            self.assertEqual(response.status, 206)
                            self.assertEqual(response.headers["Content-Range"], "bytes 2-4/8")
                            self.assertEqual(response.read(), b"cde")
                    finally:
                        allow_clip.set()
                        server.shutdown()
                        thread.join(timeout=4)


if __name__ == "__main__":
    unittest.main()
