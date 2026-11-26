#!/usr/bin/env python3
"""Verify both Oshified recipes use the inherited stage runner and name their outputs."""

from itertools import product
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import ModuleType
import unittest
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from comfy_support import validate_input
from pipeline import oshified, runner
with patch.dict(sys.modules, {"runpod": ModuleType("runpod")}):
    import rp_handler


class Relay:
    podId = "fixture"

    def __init__(self):
        self.events = []

    def send_progress(self, event, data):
        self.events.append((event, data))


class OshifiedPipelineTests(unittest.TestCase):
    def test_both_recipes_return_the_still_and_clip(self):
        for entrypoint, model, (method, recipe), variant in product(
            ("local", "runpod"), ("kontext", "qwen"), oshified.RECIPES.items(), ("female", "male")
        ):
            with self.subTest(entrypoint=entrypoint, model=model, method=method, variant=variant), \
                    TemporaryDirectory() as directory:
                root = Path(directory)
                files = {slot: str(root / (slot + ".png")) for slot in oshified.REQUIRED_FILES}
                files["in_audio"] = str(root / "audio.wav")
                for slot in ("in_pose", "in_image"):
                    Image.new("RGB", (64, 48)).save(files[slot])
                raw = {
                    "generation_id": "oshified_fixture",
                    "params": {
                        "method": method, "variant": variant, "files": files,
                        "prompt": "Untrusted prompt", "style_lora": "Untrusted adapter",
                        "portrait_model": "qwen" if model == "kontext" else "kontext",
                    },
                }
                job, error = validate_input(raw)
                self.assertIsNone(error)
                outputs = ("portrait.png", "still.jpg", "clip.mp4")
                observed = []
                delivered = []

                def stage_result(graph, info, *unused):
                    index = len(observed)
                    observed.append(graph)
                    self.assertFalse(info["params_unresolved"])
                    name = outputs[index]
                    (root / name).touch()
                    prompt_id = str(index)
                    return prompt_id, {prompt_id: {
                        "outputs": {info["output_node"]: {"files": [{"filename": name}]}}
                    }}

                relay = Relay()
                expected = {"still": str(root / "still.jpg"), "clip": str(root / "clip.mp4")}

                def deliver(kind, path):
                    delivered.append((kind, path))

                with (
                    patch.object(runner, "WORK_DIR", str(root)),
                    patch.object(runner, "COMFY_OUTPUT_DIR", str(root)),
                    patch.object(runner, "download_files", return_value=files.copy()),
                    patch.object(runner, "_run_stage", side_effect=stage_result),
                    patch.object(rp_handler, "check_server"),
                    patch.object(rp_handler, "ENABLE_WEBSOCKET_PROGRESS", False),
                    patch.object(rp_handler, "WebSocketProgressRelay", return_value=relay),
                    patch.object(rp_handler, "OshifiedDelivery", return_value=deliver) as delivery,
                    patch.dict(os.environ, {"MODEL_STORE_MANIFEST": "", "OSHIFIED_PORTRAIT_MODEL": model}),
                ):
                    if entrypoint == "runpod":
                        response = rp_handler.handler({"input": raw})
                        self.assertNotIn("error", response)
                        self.assertEqual(response["output"]["message"]["artifacts"], sorted(expected))
                        delivery.assert_called_once_with(job["generation_id"], 1, relay)
                    else:
                        result = runner.run_pipeline(
                            job["generation_id"], job["user_id"], job["params"], relay, None,
                            on_artifact=deliver,
                        )
                        self.assertEqual(result["artifacts"], expected)
                        self.assertEqual(result["final_output"], expected["clip"])

                portrait = observed[0]
                if model == "qwen":
                    self.assertEqual(portrait["328"]["inputs"]["prompt"], recipe["qwen_prompts"][variant])
                    self.assertEqual(portrait["358"]["inputs"]["images"], ["338", 0])
                    self.assertNotIn("194", portrait)
                    self.assertNotIn("197", portrait)
                    self.assertNotIn("style_lora", job["params"])
                else:
                    self.assertEqual(portrait["193"]["inputs"]["text"], recipe["prompts"][variant])
                    self.assertEqual(portrait["197"]["inputs"]["lora_name"], recipe["lora"])
                for node, slot in (("322", "in_pose"), ("342", "in_image")):
                    self.assertEqual(observed[0][node]["class_type"], "LoadImage")
                    self.assertEqual(observed[0][node]["inputs"]["image"], files[slot])
                self.assertEqual(observed[1]["1"]["inputs"]["image"], str(root / "portrait.png"))
                self.assertEqual(observed[2]["187"]["inputs"]["image"], str(root / "portrait.png"))
                self.assertEqual(observed[2]["345"]["inputs"]["audio_file"], files["in_audio"])
                self.assertEqual(delivered, [("portrait", str(root / "portrait.png")), *expected.items()])

    def test_staged_image_preserves_orientation_before_core_load(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            files = {slot: str(root / f"{slot}.bin") for slot in ("in_pose", "in_image")}
            image = Image.new("RGB", (48, 64))
            image.save(files["in_pose"], format="PNG")
            orientation = image.getexif()
            orientation[274] = 6
            image.save(files["in_image"], format="PNG", exif=orientation)
            oshified.orient_staged_images(files)
            self.assertEqual(files["in_pose"], str(root / "in_pose.bin"))
            with Image.open(files["in_image"]) as oriented:
                self.assertEqual(oriented.size, (64, 48))
                self.assertEqual(oriented.getexif().get(274, 1), 1)

    def test_animation_retry_loads_only_the_clip_stage(self):
        files = {slot: "fixture" for slot in oshified.RETRY_FILES}
        job, error = validate_input({
            "generation_id": "oshified_fixture",
            "params": {"method": "real-to-vtuber", "retry_clip": True, "attempt": 2,
                       "video_url": "https://youtu.be/mockfixture", "files": files},
        })
        self.assertIsNone(error)
        for model in ("kontext", "qwen"):
            manifest = oshified.load_manifest(job["params"]["retry_clip"], model)
            self.assertEqual([stage["name"] for stage in manifest["stages"]], ["clip"])
            self.assertEqual(manifest["stages"][0]["artifact"], "clip")

    def test_portrait_model_is_an_operator_setting(self):
        with patch.dict(os.environ):
            os.environ.pop("OSHIFIED_PORTRAIT_MODEL", None)
            self.assertEqual(oshified.configured_portrait_model(), "kontext")
            os.environ["OSHIFIED_PORTRAIT_MODEL"] = "unsupported"
            with self.assertRaisesRegex(ValueError, "OSHIFIED_PORTRAIT_MODEL"):
                oshified.configured_portrait_model()

    def test_rejects_unrecognized_file_slots(self):
        _, error = validate_input({
            "generation_id": "oshified_fixture",
            "params": {"method": "vtuber-to-real", "files": {"../bad": "fixture"}},
        })
        self.assertEqual(error, "Unsupported Oshified file slot")


if __name__ == "__main__":
    unittest.main()
