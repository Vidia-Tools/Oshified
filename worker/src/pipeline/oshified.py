"""Server-owned mode choices for the Oshified workflows."""

import json
import os
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


def configured_portrait_model():
    model = os.environ.get("OSHIFIED_PORTRAIT_MODEL", "kontext")
    if model not in ("kontext", "qwen"):
        raise ValueError("Set OSHIFIED_PORTRAIT_MODEL to kontext or qwen")
    return model


def apply_recipe(params, portrait_model="kontext"):
    recipe = RECIPES[params["method"]]
    variant = params.get("variant", "female")
    if portrait_model == "qwen":
        params["prompt"] = recipe["qwen_prompts"][variant]
        params.pop("style_lora", None)
    else:
        params["prompt"] = recipe["prompts"][variant]
        params["style_lora"] = recipe["lora"]
    params["video_url"] = "" if params["files"].get("in_audio") else params["video_url"]



def orient_staged_images(files):
    """Preserve the portrait recipe's EXIF orientation before core LoadImage."""
    from PIL import Image, ImageOps

    for slot in ("in_pose", "in_image"):
        source = Path(files[slot])
        with Image.open(source) as image:
            if image.getexif().get(274, 1) not in range(2, 9):
                continue
            # Core LoadImage does not apply all orientation tags for PNG or WebP.
            # Write an oriented, lossless copy without changing the original upload.
            destination = source.with_name(source.name + ".oriented.png")
            temporary = destination.with_suffix(".partial")
            ImageOps.exif_transpose(image).save(temporary, format="PNG")
            os.replace(temporary, destination)
        files[slot] = str(destination)


def load_manifest(retry_clip=False, portrait_model="kontext"):
    with (_RECIPE_DIR / "oshified-manifest.json").open() as source:
        manifest = json.load(source)
    if retry_clip:
        manifest["stages"] = [stage for stage in manifest["stages"] if stage["name"] == "clip"]
    elif portrait_model == "qwen":
        portrait = next(stage for stage in manifest["stages"] if stage["name"] == "portrait")
        portrait["file"] = "workflows/oshified_portrait_qwen.json"
    return manifest
