"""Opt-in bridge from the committed Oshified browser contract to trusted recipes."""

from hashlib import sha256
import json
import os
from pathlib import Path
import re
from urllib.parse import parse_qs, quote, unquote, urlsplit

import requests

from comfy_support import WebSocketProgressRelay
from oshified_delivery import ArtifactDeliveryError, OshifiedDelivery


# Worker 6e00db1 generates these exact prompt/LoRA combinations server-side.
_LEGACY_RECIPES = {
    ("vtuberToReal V1.safetensors", "0339d7686090d3fc8c0af4de004d2d5842fa97d12b610f1d04d9c29d4a1a8bf0"): ("vtuber-to-real", "female"),
    ("vtuberToReal V1.safetensors", "90f501089e5a45edae50dda8afb63f3a78ff86d28a530577f186099b5744616d"): ("vtuber-to-real", "male"),
    ("realToVtuber V1.safetensors", "f9769ea9e7782ea9d79675fc36555c4fa6dd89f4b3fe1cbd15b8a78075eba719"): ("real-to-vtuber", "female"),
    ("realToVtuber V1.safetensors", "5d9b35b7f1b9b1d606d7f5461cd6874336091f99173d0ce4388e4e89e34ea8bb"): ("real-to-vtuber", "male"),
}
_ASSETS = {
    "in_pose": "OSHIFIED_POSE_URL",
    "in_end_card": "OSHIFIED_END_CARD_URL",
    "in_corner_watermark": "OSHIFIED_CORNER_WATERMARK_URL",
    "in_corner_mask": "OSHIFIED_CORNER_MASK_URL",
}
_IMAGE_SUFFIXES = {".jpeg", ".jpg", ".png", ".webp"}
_AUDIO_SUFFIXES = {".aac", ".flac", ".m4a", ".mp3", ".mp4", ".ogg", ".wav", ".webm"}
_OUTPUTS = {"still": ("-still.jpg", "image/jpeg", "imageReady", "imageUrl"),
            "clip": (".mp4", "video/mp4", "videoReady", "videoUrl")}


def _https(value):
    if (not isinstance(value, str) or not value or len(value) > 4096
            or any(ord(character) < 32 for character in value)
            or "\\" in value):
        raise ValueError("Invalid legacy media URL")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.port is not None or parsed.fragment
            or parsed.netloc != parsed.hostname or not re.fullmatch(r"[a-z0-9.-]+", parsed.hostname)
            or any(character.isspace() for character in parsed.query)):
        raise ValueError("Invalid legacy media URL")
    return parsed


def _configured_origin(name):
    value = os.environ.get(name, "").rstrip("/")
    parsed = _https(value)
    if parsed.path or parsed.query:
        raise ValueError("Legacy media origins must be HTTPS origins")
    return value


def _upload(value, generation_id, origin, suffixes):
    parsed = _https(value)
    path = unquote(parsed.path)
    segments = path.split("/")
    if (f"https://{parsed.hostname}" != origin or parsed.query or len(segments) != 3
            or segments[1] != generation_id or "\\" in path
            or any(ord(character) < 32 for character in path)
            or Path(segments[2]).suffix.lower() not in suffixes):
        raise ValueError("Invalid legacy upload")
    # The legacy uploader can return the original filename with literal spaces.
    return f"{origin}{quote(path, safe='/')}"


def _youtube(parsed):
    if parsed.hostname == "youtu.be":
        video_id = parsed.path.removeprefix("/")
    elif parsed.hostname in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        if parsed.path == "/watch":
            ids = parse_qs(parsed.query).get("v", [])
            video_id = ids[0] if len(ids) == 1 else ""
        elif parsed.path.startswith("/shorts/"):
            video_id = parsed.path.removeprefix("/shorts/")
        else:
            video_id = ""
    else:
        return None
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id) is None:
        raise ValueError("Invalid legacy YouTube link")
    return f"https://www.youtube.com/watch?v={video_id}"


def _widget(graph, node_id, class_type, key):
    node = graph.get(node_id)
    if not isinstance(node, dict) or node.get("class_type") != class_type:
        raise ValueError("Unexpected legacy workflow node")
    inputs = node.get("inputs")
    if not isinstance(inputs, dict) or not isinstance(inputs.get(key), str):
        raise ValueError("Missing legacy workflow input")
    return inputs[key]


def adapt(job_input):
    if os.environ.get("OSHIFIED_LEGACY_ROLLBACK") != "1":
        raise ValueError("Legacy Oshified input is disabled")
    if set(job_input) != {"prompt", "client_id", "generation_type"} or job_input["generation_type"] != "full":
        raise ValueError("Unexpected legacy Oshified envelope")
    generation_id = job_input["client_id"]
    if not isinstance(generation_id, str) or re.fullmatch(r"oshified_[0-9]{13}_[a-z0-9]{1,9}", generation_id) is None:
        raise ValueError("Invalid legacy Oshified generation ID")
    graph = job_input["prompt"]
    if not isinstance(graph, dict) or len(graph) > 128:
        raise ValueError("Invalid legacy Oshified guidance")
    prompt = _widget(graph, "193", "CLIPTextEncode", "text")
    lora = _widget(graph, "197", "LoraLoaderModelOnly", "lora_name")
    recipe = _LEGACY_RECIPES.get((lora, sha256(prompt.encode()).hexdigest()))
    if recipe is None:
        raise ValueError("Unknown legacy Oshified mode or variant")
    if (generation_id != _widget(graph, "358", "OshifiedImageSaver", "filename_prefix")
            or generation_id != _widget(graph, "352", "OshifiedVideoSaver", "filename_prefix")):
        raise ValueError("Legacy Oshified output ID mismatch")

    inputs_origin = _configured_origin("OSHIFIED_LEGACY_UPLOAD_ORIGIN")
    assets = {slot: os.environ.get(name, "") for slot, name in _ASSETS.items()}
    for value in assets.values():
        _https(value)
    image = _widget(graph, "342", "LoadImageFromUrlOrPath", "url_or_path")
    _https(image)
    presets = json.loads(os.environ.get("OSHIFIED_LEGACY_IMAGE_PRESETS", "[]"))
    if not isinstance(presets, list) or not all(isinstance(value, str) for value in presets):
        raise ValueError("Legacy image presets must be an exact URL list")
    assets["in_image"] = image if image in presets else _upload(image, generation_id, inputs_origin, _IMAGE_SUFFIXES)
    audio = _widget(graph, "345", "OshifiedAudioImporter", "video_url")
    youtube = _youtube(_https(audio))
    if youtube is None:
        assets["in_audio"] = _upload(audio, generation_id, inputs_origin, _AUDIO_SUFFIXES)
    method, variant = recipe
    return {"generation_id": generation_id, "user_id": generation_id,
            "params": {"method": method, "variant": variant, "files": assets, "video_url": youtube}}


class LegacyRelay(WebSocketProgressRelay):
    def __init__(self, generation_id):
        super().__init__(generation_id)
        self.backend = _configured_origin("BACKEND_BASE")
        if not self.callback_secret:
            raise ValueError("Legacy worker callback secret is required")
        self.websocket_relay_url = self.backend + "/api/runpod/progress"

    def _post(self, url, payload):
        try:
            response = requests.post(url, json=payload, timeout=10, allow_redirects=False,
                                     headers={"X-Callback-Secret": self.callback_secret})
            return response.status_code == 200
        except requests.RequestException:
            return False

    def send_progress(self, event_type, progress_data):
        # Legacy marks any executed event terminal. Its two media callbacks
        # alone must complete the job.
        if event_type not in ("heartbeat", "model_preparation", "error"):
            return True
        if event_type == "error":
            progress_data = {"message": "Generation failed"}
        return self._post(self.websocket_relay_url, {
            "generationID": self.generation_id, "eventType": event_type, "progressData": progress_data,
        })

    def send_terminal_logs(self, log_content, user_id=None):
        return True

    def notify_media(self, route, url_key, url):
        return self._post(f"{self.backend}/api/runpod/{route}",
                          {"generationID": self.generation_id, url_key: url})


class LegacyDelivery(OshifiedDelivery):
    def __init__(self, generation_id, relay):
        super().__init__(generation_id, 1, relay)
        self.export_base = _configured_origin("S3_PUBLIC_URL_PREFIX")

    def __call__(self, artifact, file_path):
        if artifact == "portrait":  # lossless intermediate remains local
            return
        suffix, content_type, route, url_key = _OUTPUTS[artifact]
        path = Path(file_path)
        if not path.is_file() or path.suffix.lower() != Path("result" + suffix).suffix:
            raise ArtifactDeliveryError(f"Missing {artifact} output")
        key = self.generation_id + suffix
        try:
            if self.client is None:
                self._configure_storage()
            self.client.upload_file(str(path), self.bucket, key, ExtraArgs={"ContentType": content_type})
        except Exception as exc:
            raise ArtifactDeliveryError(f"Could not upload the {artifact}") from exc
        for _ in range(2):
            if self.relay.notify_media(route, url_key, f"{self.export_base}/{key}"):
                return
        raise ArtifactDeliveryError(f"Could not register the {artifact} output")
