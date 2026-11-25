"""Deliver Oshified results to the configured exports bucket."""

import os
from pathlib import Path

_MEDIA = {"portrait": ("png", "image/png"), "still": ("jpg", "image/jpeg"), "clip": ("mp4", "video/mp4")}


class ArtifactDeliveryError(RuntimeError):
    pass


class OshifiedDelivery:
    def __init__(self, generation_id, attempt, relay):
        self.generation_id = generation_id
        self.attempt = attempt
        self.relay = relay
        self.client = None
        self.bucket = None

    def _configure_storage(self):
        import boto3

        self.bucket = os.environ["S3_BUCKET_NAME"]
        self.client = boto3.client(
            "s3",
            endpoint_url=os.environ["S3_ENDPOINT_URL"],
            aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
            region_name=os.getenv("S3_REGION_NAME", "auto"),
        )

    def __call__(self, artifact, file_path):
        extension, content_type = _MEDIA[artifact]
        path = Path(file_path)
        if path.suffix.lower() != f".{extension}" or not path.is_file():
            raise ArtifactDeliveryError(f"Missing {artifact} output")
        key = f"oshified/{self.generation_id}/attempt-{self.attempt}/{artifact}.{extension}"
        try:
            if self.client is None:
                self._configure_storage()
            self.client.upload_file(str(path), self.bucket, key, ExtraArgs={"ContentType": content_type})
        except Exception as exc:
            raise ArtifactDeliveryError(f"Could not upload the {artifact}") from exc
        for _ in range(2):
            if self.relay.send_progress("oshified_artifact", {"artifact": artifact, "key": key}):
                return
        raise ArtifactDeliveryError(f"Could not register the {artifact} output")
