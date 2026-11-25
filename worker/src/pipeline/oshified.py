"""Server-owned mode choices for the Oshified workflows."""

import json
from pathlib import Path
import re

_RECIPE_DIR = Path(__file__).resolve().parent
with (_RECIPE_DIR / "oshified-recipes.json").open() as source:
    RECIPES = json.load(source)

CLIP_FILES = ("in_end_card", "in_corner_watermark", "in_corner_mask")
REQUIRED_FILES = ("in_image", "in_pose", *CLIP_FILES)
RETRY_FILES = ("in_portrait", *CLIP_FILES)


def validate_job(generation_id, params):
    if not isinstance(generation_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", generation_id) is None:
        return "Invalid Oshified generation ID"
    if params.get("variant", "female") not in ("female", "male"):
        return "Invalid Oshified portrait variant"
    retry = params.get("retry_clip", False)
    if type(retry) is not bool:
        return "Invalid Oshified retry flag"
    attempt = params.get("attempt", 1)
    if type(attempt) is not int or attempt != (2 if retry else 1):
        return "Invalid Oshified attempt"
    files = params.get("files", {})
    required = RETRY_FILES if retry else REQUIRED_FILES
    if any(slot not in (*required, "in_audio") for slot in files):
        return "Unsupported Oshified file slot"
    if any(not isinstance(files.get(slot), str) or not files[slot] for slot in required):
        return "Missing required Oshified image or watermark assets"
    if files.get("in_audio"):
        if not isinstance(files["in_audio"], str):
            return "Invalid Oshified audio file"
    elif not isinstance(params.get("video_url"), str) or not params["video_url"]:
        return "Oshified requires audio or a YouTube link"
    return None


def apply_recipe(params):
    recipe = RECIPES[params["method"]]
    params["prompt"] = recipe["prompts"][params.get("variant", "female")]
    params["style_lora"] = recipe["lora"]
    params["video_url"] = "" if params["files"].get("in_audio") else params["video_url"]


def load_manifest(retry_clip=False):
    with (_RECIPE_DIR / "oshified-manifest.json").open() as source:
        manifest = json.load(source)
    if retry_clip:
        manifest["stages"] = [stage for stage in manifest["stages"] if stage["name"] == "clip"]
    return manifest
