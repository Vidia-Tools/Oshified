#!/usr/bin/env python
"""Local mode entrypoint: a thin HTTP server wrapping the pipeline engine.

No RunPod SDK, no R2 uploads, and no backend callbacks. The engine
(src/pipeline/) is identical to RunPod mode; only the wrapper differs.

Endpoints:
  POST /generate            section-3 params payload -> runs pipeline,
                            returns {"output_file": "..."} (path in the
                            ComfyUI output directory)
  GET  /progress?id=<gen>   Server-Sent Events stream of the same stage
                            events the RunPod relay emits (stage_start,
                            executing, progress, executed, stage_complete,
                            error, ...). Omit id to receive all generations.

  /api/oshified/jobs       Loopback-only jobs, uploaded inputs and media.

Usage:
  python app_server.py --comfy http://127.0.0.1:8188 --port 8189
"""

import argparse
from email import policy
from email.parser import BytesParser
from ipaddress import ip_address
import json
import os
from pathlib import Path
import queue
import random
import re
import secrets
from tempfile import TemporaryDirectory
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


def _parse_args():
    parser = argparse.ArgumentParser(description="Vidia Open Studio local app server")
    parser.add_argument("--comfy", default=os.environ.get("COMFY_URL", "http://127.0.0.1:8188"),
                        help="ComfyUI base URL (default http://127.0.0.1:8188)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("APP_SERVER_PORT", 8189)),
                        help="Port for this app server (default 8189)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Bind address (default 127.0.0.1; use 0.0.0.0 to "
                             "expose on all interfaces)")
    return parser.parse_args()


ARGS = _parse_args()
# comfy_support reads COMFY_HOST at import time, so set it before importing.
os.environ["COMFY_HOST"] = urlparse(ARGS.comfy).netloc or ARGS.comfy

from comfy_support import (  # noqa: E402
    COMFY_API_AVAILABLE_INTERVAL_MS, COMFY_API_AVAILABLE_MAX_RETRIES,
    COMFY_HOST, ENABLE_WEBSOCKET_PROGRESS, WS_READY_TIMEOUT,
    ProgressTracker, check_server, logger, validate_input,
)
from pipeline import runner  # noqa: E402
import build_mode  # noqa: E402

BUILD_PREFIX = "/build/"


class LocalProgressRelay:
    """Drop-in replacement for WebSocketProgressRelay that fans events out to
    SSE subscribers instead of POSTing to a backend. StageRelay wraps it
    unchanged, so events carry the same stageName/stageIndex/stageTotal."""

    def __init__(self):
        self.podId = "local"
        self._lock = threading.Lock()
        self._subscribers = []  # list of (generation_id filter or None, queue)

    def subscribe(self, generation_id=None):
        q = queue.Queue()
        with self._lock:
            self._subscribers.append((generation_id, q))
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._subscribers = [(g, s) for g, s in self._subscribers if s is not q]

    def for_generation(self, generation_id, on_event=None):
        return _GenerationRelay(self, generation_id, on_event)

    def publish(self, generation_id, event_type, progress_data):
        event = {"generation_id": generation_id, "eventType": event_type,
                 "progressData": progress_data}
        with self._lock:
            for gen_filter, q in self._subscribers:
                if gen_filter is None or gen_filter == generation_id:
                    q.put(event)
        return True


class _GenerationRelay:
    """Per-generation view exposing the WebSocketProgressRelay interface."""

    def __init__(self, hub, generation_id, on_event):
        self._hub = hub
        self._on_event = on_event
        self.generation_id = generation_id
        self.podId = hub.podId

    def send_progress(self, event_type, progress_data):
        if self._on_event:
            self._on_event(event_type, progress_data)
        return self._hub.publish(self.generation_id, event_type, progress_data)

    def notify_video_ready(self, video_url):
        return self._hub.publish(self.generation_id, "video_ready", {"videoUrl": video_url})

    def send_terminal_logs(self, log_content, user_id=None):
        return self._hub.publish(self.generation_id, "terminal_logs",
                                 {"terminalOutput": log_content})


HUB = LocalProgressRelay()


def run_generation(validated, on_event=None, on_artifact=None):
    """Run one generation through the pipeline. Returns the runner result."""
    generation_id = validated["generation_id"]
    relay = HUB.for_generation(generation_id, on_event)

    progress = None
    if ENABLE_WEBSOCKET_PROGRESS:
        progress = ProgressTracker(relay)
        deadline = time.time() + WS_READY_TIMEOUT
        while time.time() < deadline:
            if progress.connect():
                break
            time.sleep(1)
        else:
            logger.info(f"Warning: ComfyUI WebSocket not ready within {WS_READY_TIMEOUT}s; "
                        "proceeding without WS gating")

    try:
        return runner.run_pipeline(generation_id, validated["user_id"],
                                   validated["params"], relay, progress,
                                   run_type=validated.get("type", "full"),
                                   on_artifact=on_artifact)
    finally:
        if progress:
            try:
                progress.stop()
            except Exception:
                pass


OSHIFIED_JOBS = "/api/oshified/jobs"
LOCAL_ASSETS = {
    "in_pose": "OSHIFIED_POSE_FILE",
    "in_end_card": "OSHIFIED_END_CARD_FILE",
    "in_corner_watermark": "OSHIFIED_CORNER_WATERMARK_FILE",
    "in_corner_mask": "OSHIFIED_CORNER_MASK_FILE",
}
IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
AUDIO_TYPES = {
    "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav",
    "audio/mp4": ".m4a", "audio/ogg": ".ogg", "audio/webm": ".webm",
}
OUTPUT_TYPES = {"still": ("image/jpeg", "portrait.jpg"), "clip": ("video/mp4", "clip.mp4")}
MAX_IMAGE = 10 * 1024 * 1024
MAX_AUDIO = 15 * 1024 * 1024
LOCAL_JOBS = {}
LOCAL_JOBS_LOCK = threading.Lock()


def _loopback(host):
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _youtube_link(link):
    url = urlparse(link)
    if url.scheme != "https" or url.username or url.password:
        return False
    if url.hostname == "youtu.be":
        video_id = url.path.strip("/")
    elif url.hostname in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        video_id = ((parse_qs(url.query).get("v") or [""])[0]
                    if url.path == "/watch" else url.path.removeprefix("/shorts/").strip("/")
                    if url.path.startswith("/shorts/") else "")
    else:
        return False
    return bool(re.fullmatch(r"[\w-]{11}", video_id, flags=re.ASCII))


def _legacy_asset_paths():
    assets = {}
    for slot, variable in LOCAL_ASSETS.items():
        value = os.environ.get(variable)
        if not value:
            raise ValueError(f"Set {variable} to a local file")
        path = Path(value).expanduser()
        if not path.is_file():
            raise ValueError(f"{variable} must point to a local file")
        assets[slot] = path.resolve()
    return assets


def _run_local_job(job, validated):
    def on_event(event, data):
        if event == "model_preparation":
            progress = {key: data[key] for key in ("phase", "stageName", "bytesReceived", "totalBytes") if key in data}
        elif event == "stage_start":
            progress = {"phase": "generating", "stageName": data.get("stageName")}
        else:
            return
        with LOCAL_JOBS_LOCK:
            job["status"] = "running"
            job["progress"] = progress

    def on_artifact(kind, location):
        if kind in OUTPUT_TYPES:
            path = Path(location).resolve()
            if not path.is_relative_to(Path(runner.COMFY_OUTPUT_DIR).resolve()) or not path.is_file():
                raise ValueError("Invalid generation output")
            with LOCAL_JOBS_LOCK:
                job["media"][kind] = path

    try:
        run_generation(validated, on_event=on_event, on_artifact=on_artifact)
        if not all(kind in job["media"] for kind in OUTPUT_TYPES):
            raise ValueError("Generation did not produce both outputs")
        with LOCAL_JOBS_LOCK:
            job["status"] = "complete"
    except (ValueError, runner.PipelineError) as error:
        with LOCAL_JOBS_LOCK:
            job["status"] = "failed"
            job["error"] = str(error)
    except Exception as error:
        logger.info(f"[LOCAL OSHIFIED] Generation raised {type(error).__name__}")
        with LOCAL_JOBS_LOCK:
            job["status"] = "failed"
            job["error"] = "Generation failed. Check the local worker log."
    finally:
        with LOCAL_JOBS_LOCK:
            job["inputs"] = {}


class AppHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, status, payload, origin="*"):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if origin != "*":
            self.send_header("Cache-Control", "private, no-store")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        """Parse a JSON request body, or None if absent/invalid."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not length:
                return None
            return json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            return None

    def do_OPTIONS(self):
        if urlparse(self.path).path.startswith(OSHIFIED_JOBS):
            allowed, origin = self._local_oshified_request()
            if not allowed:
                return
            self.send_response(204)
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_PUT(self):
        parsed = urlparse(self.path)
        if not parsed.path.startswith(BUILD_PREFIX):
            self._json(404, {"error": "Not found"})
            return
        sub = parsed.path[len(BUILD_PREFIX):]
        status, payload = build_mode.dispatch(
            "PUT", sub, parse_qs(parsed.query), self._read_body())
        self._json(status, payload)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == OSHIFIED_JOBS:
            allowed, origin = self._local_oshified_request()
            if allowed:
                self._submit_oshified(origin)
            return
        if parsed.path.startswith(BUILD_PREFIX):
            sub = parsed.path[len(BUILD_PREFIX):]
            status, payload = build_mode.dispatch(
                "POST", sub, parse_qs(parsed.query), self._read_body())
            self._json(status, payload)
            return
        if parsed.path != "/generate":
            self._json(404, {"error": "Not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"error": "Invalid JSON body", "generation_id": None,
                             "stage": None})
            return

        # "client_id" accepted as an alias at this entry boundary only;
        # mint a generation_id when the client supplies neither.
        if isinstance(payload, dict):
            if not payload.get("generation_id") and payload.get("client_id"):
                payload["generation_id"] = payload["client_id"]
            if not payload.get("generation_id"):
                payload["generation_id"] = (
                    f"gen_{int(time.time())}_{random.getrandbits(32):08x}")

        validated, error = validate_input(payload)
        if error:
            self._json(400, {"error": error,
                             "generation_id": payload.get("generation_id")
                             if isinstance(payload, dict) else None,
                             "stage": None})
            return

        generation_id = validated["generation_id"]
        logger.info(f"[LOCAL] generate start generation_id={generation_id}")
        try:
            result = run_generation(validated)
            self._json(200, {"status": "success",
                             "generation_id": generation_id,
                             "output_file": result.get("final_output"),
                             "artifacts": result.get("artifacts", {}),
                             "resolved_seed": result.get("resolved_seed"),
                             "stages": result.get("stages")})
        except runner.PipelineError as e:
            HUB.publish(generation_id, "error",
                        {"error": str(e), "generation_id": generation_id,
                         "stage": e.stage})
            self._json(500, {"error": str(e), "generation_id": generation_id,
                             "stage": e.stage})
        except Exception as e:
            self._json(500, {"error": f"Unexpected error: {e}",
                             "generation_id": generation_id, "stage": None})

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith(OSHIFIED_JOBS + "/"):
            allowed, origin = self._local_oshified_request()
            if allowed:
                self._get_oshified(parsed, origin)
            return
        if parsed.path.startswith(BUILD_PREFIX):
            sub = parsed.path[len(BUILD_PREFIX):]
            status, payload = build_mode.dispatch(
                "GET", sub, parse_qs(parsed.query), None)
            self._json(status, payload)
            return
        if parsed.path != "/progress":
            self._json(404, {"error": "Not found"})
            return
        generation_id = (parse_qs(parsed.query).get("id") or [None])[0]
        q = HUB.subscribe(generation_id)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            while True:
                try:
                    event = q.get(timeout=15)
                    data = json.dumps(event)
                except queue.Empty:
                    data = json.dumps({"eventType": "heartbeat",
                                       "progressData": {"ts": int(time.time() * 1000)}})
                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            HUB.unsubscribe(q)

    def _local_oshified_request(self):
        """Require loopback bind, loopback client and host, and a trusted browser origin."""
        origin = self.headers.get("Origin")
        port = self.server.server_port
        try:
            host = urlparse("//" + self.headers.get("Host", ""))
            verified = all(map(_loopback, (self.server.server_address[0], self.client_address[0], host.hostname)))
            verified = verified and not host.username and not host.password and host.port == port
        except ValueError:
            verified = False
        frontend_port = os.environ.get("OSHIFIED_LOCAL_FRONTEND_PORT", "3000")
        origins = {f"http://{name}:{value}" for name in ("127.0.0.1", "localhost")
                   for value in (str(port), frontend_port)}
        if not verified or (origin and origin not in origins):
            self.close_connection = True
            self._json(403, {"message": "Oshified local jobs require a loopback connection"}, origin=None)
            return False, None
        return True, origin

    def _submit_oshified(self, origin):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_IMAGE + MAX_AUDIO + 16 * 1024:
                raise ValueError("Upload size is missing or exceeds the limit")
            if self.headers.get_content_type() != "multipart/form-data":
                raise ValueError("Use a multipart form")
            envelope = f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode("ascii")
            form = BytesParser(policy=policy.default).parsebytes(envelope + self.rfile.read(length))
            if not form.is_multipart():
                raise ValueError("Invalid multipart form")
            parts = {part.get_param("name", header="content-disposition"): part for part in form.iter_parts()}
            text = {name: parts[name].get_payload(decode=True).decode("utf-8")
                    for name in ("mode", "variant", "video_url") if name in parts}
            uploads = {}
            for name, limit, media_types in (("image", MAX_IMAGE, IMAGE_TYPES), ("audio", MAX_AUDIO, AUDIO_TYPES)):
                part = parts.get(name)
                payload = part.get_payload(decode=True) if part else None
                if not payload and name == "audio":
                    continue
                suffix = media_types.get(part.get_content_type()) if part else None
                if not payload or not suffix or len(payload) > limit:
                    raise ValueError(f"Invalid {name} upload")
                uploads[f"in_{name}"] = (suffix, payload)
            if "in_audio" not in uploads and not _youtube_link(text.get("video_url", "")):
                raise ValueError("Upload audio or enter a valid YouTube URL")
            sources = _legacy_asset_paths()
        except ValueError as error:
            self.close_connection = True
            self._json(400, {"message": str(error)}, origin)
            return
        generation_id = "oshified_" + secrets.token_hex(16)
        access = secrets.token_hex(32)
        input_access = secrets.token_hex(32)
        base = f"http://127.0.0.1:{self.server.server_port}{OSHIFIED_JOBS}/{generation_id}"
        suffixes = {slot: path.suffix for slot, path in sources.items()}
        suffixes.update({slot: suffix for slot, (suffix, _) in uploads.items()})
        params = {
            "method": text.get("mode"),
            "variant": text.get("variant", "female"),
            "video_url": text.get("video_url", ""),
            "files": {slot: f"{base}/inputs/{slot}{suffix}?access={input_access}"
                      for slot, suffix in suffixes.items()},
        }
        validated, error = validate_input({"generation_id": generation_id, "params": params})
        if error:
            self._json(400, {"message": error}, origin)
            return
        uploads_dir = None
        started = False
        try:
            uploads_dir = TemporaryDirectory(prefix="oshified-upload-")
            for slot, (suffix, payload) in uploads.items():
                path = Path(uploads_dir.name) / (slot + suffix)
                path.write_bytes(payload)
                sources[slot] = path
            job = {
                "status": "queued", "progress": {"phase": "starting"}, "base": base,
                "access": access, "input_access": input_access,
                "inputs": {slot + suffixes[slot]: path for slot, path in sources.items()},
                "media": {},
            }
            with LOCAL_JOBS_LOCK:
                busy = any(existing["status"] in {"queued", "running"} for existing in LOCAL_JOBS.values())
                if not busy:
                    if len(LOCAL_JOBS) >= 32:
                        LOCAL_JOBS.pop(next(iter(LOCAL_JOBS)))
                    LOCAL_JOBS[generation_id] = job
            if busy:
                self._json(409, {"message": "An Oshified job is already running"}, origin)
                return

            def run_owned_uploads():
                try:
                    _run_local_job(job, validated)
                finally:
                    uploads_dir.cleanup()

            threading.Thread(target=run_owned_uploads, daemon=True).start()
            started = True
        except (OSError, RuntimeError):
            self._json(500, {"message": "Could not stage local inputs"}, origin)
            return
        finally:
            if not started and uploads_dir:
                with LOCAL_JOBS_LOCK:
                    LOCAL_JOBS.pop(generation_id, None)
                uploads_dir.cleanup()
        self._json(202, {"id": generation_id, "access": access}, origin)

    def _get_oshified(self, parsed, origin):
        id_and_path = parsed.path.removeprefix(OSHIFIED_JOBS + "/").split("/")
        generation_id, *resource = id_and_path
        query = parse_qs(parsed.query)
        access = self.headers.get("Authorization", "").removeprefix("Bearer ") or (query.get("access") or [""])[0]
        if not re.fullmatch(r"[a-f0-9]{64}", access):
            self._json(404, {"message": "Not found"}, origin)
            return
        with LOCAL_JOBS_LOCK:
            job = LOCAL_JOBS.get(generation_id)
            if job and not resource and secrets.compare_digest(job["access"], access):
                snapshot = {"id": generation_id, "status": job["status"], "progress": job["progress"],
                            "error": job.get("error"), "canRetryClip": False,
                            "artifacts": {kind: f"{job['base']}/media/{kind}?access={access}"
                                          for kind in job["media"]}}
                resource_path = None
            elif job and len(resource) == 2 and resource[0] == "inputs" and secrets.compare_digest(job["input_access"], access):
                snapshot = None
                resource_path = job["inputs"].get(resource[1])
                metadata = ("application/octet-stream", None)
            elif job and len(resource) == 2 and resource[0] == "media" and secrets.compare_digest(job["access"], access):
                snapshot = None
                resource_path = job["media"].get(resource[1])
                metadata = OUTPUT_TYPES.get(resource[1])
            else:
                snapshot = None
                resource_path = None
        if snapshot:
            self._json(200, snapshot, origin)
        elif resource_path and metadata:
            self._send_oshified_file(resource_path, *metadata, origin, query.get("download") == ["1"])
        else:
            self._json(404, {"message": "Not found"}, origin)

    def _send_oshified_file(self, path, media_type, name, origin, download):
        try:
            source = path.open("rb")
        except OSError:
            self._json(404, {"message": "File not found"}, origin)
            return
        with source:
            size = os.fstat(source.fileno()).st_size
            start, end = 0, size - 1
            range_header = self.headers.get("Range")
            if range_header:
                span = re.fullmatch(r"bytes=(\d{0,18})-(\d{0,18})", range_header)
                if span and span[1]:
                    start, end = int(span[1]), min(end, int(span[2])) if span[2] else end
                elif span and span[2]:
                    start = max(0, size - int(span[2]))
                if not span or not (span[1] or span[2]) or not 0 <= start <= end < size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    if origin:
                        self.send_header("Access-Control-Allow-Origin", origin)
                    self.end_headers()
                    return
            remaining = end - start + 1
            self.send_response(206 if range_header else 200)
            self.send_header("Content-Type", media_type)
            self.send_header("Content-Length", str(remaining))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
            if range_header:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            if download and name:
                self.send_header("Content-Disposition", f'attachment; filename="oshified-{name}"')
            self.end_headers()
            source.seek(start)
            try:
                while remaining:
                    chunk = source.read(min(remaining, 1 << 20))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def log_message(self, fmt, *args):
        if not self.path.startswith(OSHIFIED_JOBS):
            logger.debug(f"[HTTP] {fmt % args}")


def main():
    check_server(f"http://{COMFY_HOST}",
                 COMFY_API_AVAILABLE_MAX_RETRIES,
                 COMFY_API_AVAILABLE_INTERVAL_MS)
    server = ThreadingHTTPServer((ARGS.host, ARGS.port), AppHandler)
    logger.info(f"Vidia Open Studio local server on {ARGS.host}:{ARGS.port} "
                f"(ComfyUI at {COMFY_HOST})")
    server.serve_forever()


if __name__ == "__main__":
    main()
