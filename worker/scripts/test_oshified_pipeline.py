#!/usr/bin/env python3
"""Verify both Oshified recipes use the inherited stage runner and name their outputs."""

import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from comfy_support import validate_input
from pipeline import oshified, runner


class Relay:
    podId = "fixture"

    def __init__(self):
        self.events = []

    def send_progress(self, event, data):
        self.events.append((event, data))


class OshifiedPipelineTests(unittest.TestCase):
    def test_both_recipes_return_the_still_and_clip(self):
        for method, recipe in oshified.RECIPES.items():
            with self.subTest(method=method), TemporaryDirectory() as directory:
                root = Path(directory)
                files = {slot: str(root / (slot + ".png")) for slot in oshified.REQUIRED_FILES}
                files["in_audio"] = str(root / "audio.wav")
                job, error = validate_input({
                    "generation_id": "oshified_fixture",
                    "params": {"method": method, "variant": "male", "files": files},
                })
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
                with patch.object(runner, "WORK_DIR", str(root)), \
                     patch.object(runner, "COMFY_OUTPUT_DIR", str(root)), \
                     patch.object(runner, "download_files", return_value=files.copy()), \
                     patch.object(runner, "_run_stage", side_effect=stage_result), \
                     patch.dict(os.environ, {"MODEL_STORE_MANIFEST": ""}):
                    result = runner.run_pipeline(
                        job["generation_id"], job["user_id"], job["params"], relay, None,
                        on_artifact=lambda kind, path: delivered.append((kind, path)),
                    )

                self.assertEqual(observed[0]["193"]["inputs"]["text"], recipe["prompts"]["male"])
                self.assertEqual(observed[0]["197"]["inputs"]["lora_name"], recipe["lora"])
                self.assertEqual(observed[1]["1"]["inputs"]["image"], str(root / "portrait.png"))
                self.assertEqual(observed[2]["187"]["inputs"]["image"], str(root / "portrait.png"))
                self.assertEqual(observed[2]["345"]["inputs"]["audio_file"], files["in_audio"])
                self.assertEqual(result["artifacts"], {"still": str(root / "still.jpg"), "clip": str(root / "clip.mp4")})
                self.assertEqual(result["final_output"], str(root / "clip.mp4"))
                self.assertEqual(delivered, [("portrait", str(root / "portrait.png")), *result["artifacts"].items()])

    def test_animation_retry_loads_only_the_clip_stage(self):
        files = {slot: "fixture" for slot in oshified.RETRY_FILES}
        job, error = validate_input({
            "generation_id": "oshified_fixture",
            "params": {"method": "real-to-vtuber", "retry_clip": True, "attempt": 2,
                       "video_url": "https://youtu.be/mockfixture", "files": files},
        })
        self.assertIsNone(error)
        manifest = oshified.load_manifest(job["params"]["retry_clip"])
        self.assertEqual([stage["name"] for stage in manifest["stages"]], ["clip"])
        self.assertEqual(manifest["stages"][0]["artifact"], "clip")

    def test_rejects_unrecognized_file_slots(self):
        _, error = validate_input({
            "generation_id": "oshified_fixture",
            "params": {"method": "vtuber-to-real", "files": {"../bad": "fixture"}},
        })
        self.assertEqual(error, "Unsupported Oshified file slot")


if __name__ == "__main__":
    unittest.main()
